"""
End-to-end evaluation harness driver.

Runs the agent's ReviewEngine against a fixture (or directory of fixtures),
collects FileReviewResult outputs, scores them with the metrics module, and
writes both a machine-readable JSON report and a human-readable Markdown
summary to a configurable report directory.

Usage:

    python -m code_review_agent.evaluation.runner \\
        --fixtures tests/fixtures/prs \\
        --report reports/

    python -m code_review_agent.evaluation.runner \\
        --fixture tests/fixtures/prs/py-sql-injection-001.json \\
        --report reports/

    # RAG-off ablation
    python -m code_review_agent.evaluation.runner \\
        --fixtures tests/fixtures/prs \\
        --no-rag --report reports/no-rag/

    # Consistency under repeat
    python -m code_review_agent.evaluation.runner \\
        --fixtures tests/fixtures/prs \\
        --repeats 10 --report reports/repeat/

The runner is intentionally minimal: it does NOT call GitHub, does NOT
require a webhook, and does NOT depend on the runtime path. It calls
ReviewEngine.review_file directly with the fixture's pre-loaded patch
and content, which is what makes the harness reproducible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Sequence

from code_review_agent.review_engine import FileReviewResult, ReviewEngine

from .fixtures import Fixture, load_fixture, load_fixture_directory
from .metrics import (
    compute_consistency,
    compute_correctness,
    emit_grounding_tasks,
)


async def run_one_fixture(
    fixture: Fixture,
    rag_enabled: bool = True,
) -> List[FileReviewResult]:
    """
    Run the agent against every file in a fixture.

    When rag_enabled=False the runner monkey-patches ReviewEngine.rag_system
    to return an empty guideline list, which lets us measure RAG-off behavior
    without changing prompt construction code.
    """
    engine = ReviewEngine()

    if not rag_enabled:
        async def _no_guidelines(*_args, **_kwargs):
            return []
        engine.rag_system.retrieve_guidelines = _no_guidelines  # type: ignore[assignment]

    results: list[FileReviewResult] = []
    for ff in fixture.files:
        result = await engine.review_file(
            filename=ff.path,
            patch=ff.patch,
            file_content=ff.content,
        )
        results.append(result)
    return results


async def run_repeats(
    fixture: Fixture,
    repeats: int,
    rag_enabled: bool = True,
) -> List[List[FileReviewResult]]:
    """Run the same fixture N times, returning N output sequences."""
    runs: list[list[FileReviewResult]] = []
    for _ in range(repeats):
        runs.append(await run_one_fixture(fixture, rag_enabled=rag_enabled))
    return runs


def write_reports(
    report_dir: Path,
    fixture: Fixture,
    correctness_dict: dict,
    consistency_dict: dict | None,
    grounding_tasks: list,
) -> None:
    """Write JSON + Markdown reports for one fixture."""
    report_dir.mkdir(parents=True, exist_ok=True)
    base = report_dir / fixture.fixture_id

    payload = {
        "fixture_id": fixture.fixture_id,
        "description": fixture.description,
        "correctness": correctness_dict,
    }
    if consistency_dict is not None:
        payload["consistency"] = consistency_dict
    if grounding_tasks:
        payload["grounding_tasks"] = [asdict(t) for t in grounding_tasks]

    base.with_suffix(".json").write_text(json.dumps(payload, indent=2))
    base.with_suffix(".md").write_text(_render_markdown(fixture, payload))


def _render_markdown(fixture: Fixture, payload: dict) -> str:
    out = [f"# Evaluation report — {fixture.fixture_id}", ""]
    out.append(f"**Description:** {fixture.description}")
    out.append("")
    c = payload["correctness"]
    out.append("## Correctness")
    out.append("")
    out.append(f"| Metric | Value |")
    out.append(f"|---|---|")
    out.append(f"| Precision | {c['precision']} |")
    out.append(f"| Recall | {c['recall']} |")
    out.append(f"| F1 | {c['f1']} |")
    out.append(f"| Severity-weighted recall | {c['severity_weighted_recall']} |")
    out.append(f"| TP / FP / FN | {c['true_positives']} / {c['false_positives']} / {c['false_negatives']} |")
    if c["unmatched_expected"]:
        out.append("")
        out.append("### Missed expected issues")
        out.append("")
        for ei in c["unmatched_expected"]:
            out.append(f"- {ei['file']}:{ei['line']} ({ei['category']}, {ei['severity']})")
    if "consistency" in payload:
        cc = payload["consistency"]
        out.append("")
        out.append("## Consistency under repeat")
        out.append("")
        out.append(f"| Metric | Value |")
        out.append(f"|---|---|")
        out.append(f"| Runs | {cc['runs']} |")
        out.append(f"| Mean Jaccard | {cc['mean_jaccard']} |")
        out.append(f"| Min / Max Jaccard | {cc['min_jaccard']} / {cc['max_jaccard']} |")
        out.append(f"| Severity stability | {cc['severity_stability']} |")
    return "\n".join(out) + "\n"


async def amain(args: argparse.Namespace) -> int:
    # Load fixtures
    if args.fixture:
        fixtures = [load_fixture(args.fixture)]
    elif args.fixtures:
        fixtures = load_fixture_directory(args.fixtures)
    else:
        print("Provide --fixture or --fixtures", file=sys.stderr)
        return 2

    if not fixtures:
        print("No fixtures loaded", file=sys.stderr)
        return 1

    report_dir = Path(args.report)
    rag_enabled = not args.no_rag

    summary_rows: list[tuple[str, dict, dict | None]] = []

    for fixture in fixtures:
        print(f"== {fixture.fixture_id} ==")
        if args.repeats and args.repeats > 1:
            runs = await run_repeats(fixture, args.repeats, rag_enabled=rag_enabled)
            # Score correctness on the first run only; consistency over all.
            correctness = compute_correctness(fixture, runs[0])
            consistency = compute_consistency(runs)
            grounding = emit_grounding_tasks(fixture, runs[0])
            write_reports(
                report_dir,
                fixture,
                correctness.to_dict(),
                consistency.to_dict(),
                grounding,
            )
            summary_rows.append((fixture.fixture_id, correctness.to_dict(), consistency.to_dict()))
        else:
            results = await run_one_fixture(fixture, rag_enabled=rag_enabled)
            correctness = compute_correctness(fixture, results)
            grounding = emit_grounding_tasks(fixture, results)
            write_reports(
                report_dir,
                fixture,
                correctness.to_dict(),
                None,
                grounding,
            )
            summary_rows.append((fixture.fixture_id, correctness.to_dict(), None))

    _write_summary(report_dir, summary_rows, rag_enabled, args.repeats or 1)
    return 0


def _write_summary(report_dir: Path, rows: list, rag_enabled: bool, repeats: int) -> None:
    out = ["# Evaluation summary", ""]
    out.append(f"- RAG: {'on' if rag_enabled else 'OFF (ablation)'}")
    out.append(f"- Repeats per fixture: {repeats}")
    out.append("")
    out.append("| Fixture | Precision | Recall | F1 | Severity-weighted recall |")
    out.append("|---|---|---|---|---|")
    for fid, c, _cc in rows:
        out.append(
            f"| {fid} | {c['precision']} | {c['recall']} | {c['f1']} | {c['severity_weighted_recall']} |"
        )
    (report_dir / "summary.md").write_text("\n".join(out) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluation harness for the Automated Code Review Agent."
    )
    fixture_group = parser.add_mutually_exclusive_group(required=True)
    fixture_group.add_argument(
        "--fixture",
        type=Path,
        help="Run a single fixture file.",
    )
    fixture_group.add_argument(
        "--fixtures",
        type=Path,
        help="Run every JSON fixture in this directory.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports"),
        help="Directory to write JSON + Markdown reports into.",
    )
    parser.add_argument(
        "--no-rag",
        action="store_true",
        help="Disable RAG retrieval (run the LLM with no grounding context).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Number of repeat runs per fixture (for consistency metrics).",
    )
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
