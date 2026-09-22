"""
End-to-end evaluation harness driver.

Runs the agent's ReviewEngine against a fixture (or directory of fixtures),
collects FileReviewResult outputs, scores them with the metrics module, and
writes a machine-readable JSON report and a Markdown summary per fixture,
plus a summary across fixtures.

Usage:

    # Offline, no credentials: checks the harness end to end with a
    # scanner-echo stand-in for the LLM (see evaluation/mock_llm.py).
    python -m code_review_agent.evaluation.runner \\
        --fixtures tests/fixtures/prs --mock-llm --report reports/mock/

    # Live, with Azure OpenAI credentials in the environment or .env
    python -m code_review_agent.evaluation.runner \\
        --fixtures tests/fixtures/prs --report reports/

    # RAG-off arm of the ablation
    python -m code_review_agent.evaluation.runner \\
        --fixtures tests/fixtures/prs --no-rag --report reports/no-rag/

    # Consistency under repeat
    python -m code_review_agent.evaluation.runner \\
        --fixtures tests/fixtures/prs --repeats 10 --report reports/repeat/

Each invocation runs one configuration. The ablation is two invocations
(with and without --no-rag) whose reports are compared.

Exit status: 0 on success; 1 when no fixtures are found; 2 when a live run
has no Azure credentials (argparse also exits with 2 on invalid arguments);
3 when any review call failed. Failed runs are recorded in the report and
excluded from scoring, since a failed call is not a model output.

The runner does NOT call GitHub and does NOT depend on the webhook path. It
calls ReviewEngine.review_file directly with each fixture's patch and
content.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from code_review_agent import rag_system as rag_mod
from code_review_agent import review_engine as re_mod
from code_review_agent.review_engine import FileReviewResult, ReviewEngine

from .fixtures import Fixture, load_fixture, load_fixture_directory
from .metrics import (
    DEFAULT_LINE_TOLERANCE,
    aggregate_grounding_labels,
    compute_citation_checks,
    compute_consistency,
    compute_correctness,
    emit_grounding_tasks,
    summarize_correctness,
)

# The project root: runner.py -> evaluation -> code_review_agent -> root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_LIVE_ENV = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_KEY")


def make_engine(rag_enabled: bool, mock: bool) -> ReviewEngine:
    """Build a ReviewEngine for one configuration."""
    client = None
    if mock:
        from .mock_llm import MockAzureClient

        client = MockAzureClient()
    return ReviewEngine(client=client, rag_enabled=rag_enabled)


async def run_one_fixture(fixture: Fixture, engine: ReviewEngine) -> list[FileReviewResult]:
    """Run the agent against every file in a fixture once.

    A review call that raises (network, API or content-filter error) is
    recorded on that file's result as ``call_error`` rather than ending the
    whole run, so the other files and runs still produce a report.
    """
    results: list[FileReviewResult] = []
    for ff in fixture.files:
        try:
            result = await engine.review_file(
                filename=ff.path, patch=ff.patch, file_content=ff.content
            )
        except Exception as e:  # recorded, reported, and reflected in the exit status
            print(f"  review call failed for {ff.path}: {type(e).__name__}: {e}", file=sys.stderr)
            result = FileReviewResult(
                filename=ff.path,
                summary="Review call failed.",
                call_error=f"{type(e).__name__}: {e}",
            )
        results.append(result)
    return results


async def run_repeats(
    fixture: Fixture, engine: ReviewEngine, repeats: int
) -> list[list[FileReviewResult]]:
    """Run the same fixture N times with the same engine and configuration."""
    return [await run_one_fixture(fixture, engine) for _ in range(repeats)]


def _git(*cmd: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *cmd],
            capture_output=True,
            text=True,
            check=True,
            cwd=PROJECT_ROOT,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def git_state() -> dict:
    """The commit the code came from, and whether the package, the guideline
    corpus or the fixtures differ from it, counting new untracked files there.
    Other paths, such as a reports directory, are ignored."""
    toplevel = _git("rev-parse", "--show-toplevel")
    if not toplevel or Path(toplevel.strip()).resolve() != PROJECT_ROOT:
        # An installed copy can sit inside an unrelated checkout, whose commit
        # would say nothing about the code that ran.
        return {"git_commit": None, "git_uncommitted_changes": None}
    commit = _git("rev-parse", "HEAD")
    status = _git(
        "status", "--porcelain", "--", "code_review_agent", "guidelines", "tests/fixtures"
    )
    return {
        "git_commit": commit.strip() if commit else None,
        "git_uncommitted_changes": None if status is None else bool(status.strip()),
    }


def run_metadata(args: argparse.Namespace, engine: ReviewEngine, git: dict) -> dict:
    """Everything needed to say what produced a report."""
    rag = engine.rag_system
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **git,
        "mode": (
            "mock (offline scanner-echo, not a model result)" if args.mock_llm else "azure-openai"
        ),
        "chat_deployment": None if args.mock_llm else engine.deployment,
        "embedding_deployment": None if args.mock_llm else rag.embedding_model,
        "embedding_model_reported": rag.embedding_response_model,
        "api_version": None if args.mock_llm else rag_mod.azure_api_version(),
        "temperature": re_mod.LLM_TEMPERATURE,
        "rag_enabled": engine.rag_enabled,
        "repeats": args.repeats,
        "line_tolerance": args.line_tolerance,
        "top_k": rag_mod.DEFAULT_TOP_K,
        "language_match_boost": rag_mod.LANGUAGE_MATCH_BOOST,
        "prompt_budgets_chars": {
            "guideline": re_mod.MAX_GUIDELINE_CHARS,
            "patch": re_mod.MAX_PATCH_CHARS,
            "file_content": re_mod.MAX_FILE_CONTENT_CHARS,
            "retrieval_query": rag_mod.RETRIEVAL_QUERY_CHARS,
            "embedding_input": rag_mod.MAX_EMBEDDING_INPUT_CHARS,
        },
        "scanner_findings_in_prompt": re_mod.MAX_SCANNER_FINDINGS_IN_PROMPT,
        "corpus_source": rag.corpus_source,
        "corpus_guideline_ids": [g.id for g in rag.guidelines],
        "python": platform.python_version(),
    }


def _file_provenance(runs: list[list[FileReviewResult]]) -> list:
    return [
        [
            {
                "file": r.filename,
                "retrieved_guideline_ids": r.retrieved_guideline_ids,
                "truncated_inputs": r.truncated_inputs,
                "parse_error": r.parse_error,
                "call_error": r.call_error,
                "n_comments": len(r.line_comments),
                "response_model": r.response_model,
                "raw_response": r.raw_response,
            }
            for r in run
        ]
        for run in runs
    ]


def score_fixture(
    fixture: Fixture, runs: list[list[FileReviewResult]], line_tolerance: int, meta: dict
) -> dict:
    """Score all runs of one fixture into a report payload.

    Runs with a failed review call are listed in ``failed_runs`` and left out
    of every score. The first remaining run is the reference run, used for
    the labeling tasks and the detailed Markdown sections.
    """
    failed = [i for i, run in enumerate(runs) if any(r.call_error for r in run)]
    ok = [i for i in range(len(runs)) if i not in failed]
    per_run = {i: compute_correctness(fixture, runs[i], line_tolerance) for i in ok}
    citation = {i: compute_citation_checks(fixture, runs[i], per_run[i]) for i in ok}
    ref = ok[0] if ok else None
    tasks = emit_grounding_tasks(fixture, runs[ref]) if ref is not None else []
    payload = {
        "fixture_id": fixture.fixture_id,
        "description": fixture.description,
        "runs": len(runs),
        "failed_runs": failed,
        "reference_run": ref,
        "correctness_per_run": [
            per_run[i].to_dict() if i in per_run else None for i in range(len(runs))
        ],
        # Its per_run lists follow scored_runs.
        "correctness_across_runs": {
            **summarize_correctness([per_run[i] for i in ok]),
            "scored_runs": ok,
        },
        "citation_checks_per_run": [
            citation[i].to_dict() if i in citation else None for i in range(len(runs))
        ],
        # Labeling tasks come from one run only, so a batch is labeled once
        # rather than N times.
        "grounding_tasks_reference_run": [asdict(t) for t in tasks],
        "grounding_summary_reference_run": aggregate_grounding_labels(tasks).to_dict(),
        "files_per_run": _file_provenance(runs),
    }
    payload["caveats"] = caveats(meta)
    if len(runs) > 1:
        payload["consistency"] = compute_consistency(runs, line_tolerance).to_dict()
    return payload


def caveats(meta: dict) -> list[str]:
    """Reasons a number in this report means less than it looks like.

    They are written into the report itself, not left in EVALUATION.md, so a
    score is never read without the conditions that produced it.
    """
    notes = []
    if str(meta.get("mode", "")).startswith("mock"):
        notes.append(
            "Offline mode: the stand-in echoes the static scanner's findings instead of "
            "calling a model, so these scores measure the harness, not a model, and "
            "consistency is perfect because the stand-in is deterministic."
        )
    corpus = meta.get("corpus_guideline_ids") or []
    if meta.get("rag_enabled") and corpus and meta.get("top_k", 0) >= len(corpus):
        notes.append(
            f"top_k ({meta['top_k']}) is at least the size of the loaded corpus "
            f"({len(corpus)}), so every guideline is retrieved for every file: retrieval "
            "never excludes anything and the required-guideline retrieved rate is 1.0 by "
            "construction."
        )
    if not meta.get("rag_enabled"):
        notes.append(
            "RAG is off: no guidelines reach the prompt, so citation rates are 0 by "
            "construction."
        )
    return notes


def _fmt(x) -> str:
    return "n/a" if x is None else str(x)


def _render_markdown(payload: dict, meta: dict) -> str:
    out = [f"# Evaluation report: {payload['fixture_id']}", ""]
    out.append(f"**Description:** {payload['description']}")
    out.append("")
    out.append(f"- Mode: {meta['mode']}")
    out.append(f"- RAG: {'on' if meta['rag_enabled'] else 'OFF (ablation)'}")
    out.append(f"- Repeats: {meta['repeats']}; line tolerance: ±{meta['line_tolerance']}")
    dirty = " (with uncommitted changes)" if meta.get("git_uncommitted_changes") else ""
    out.append(f"- Commit: {_fmt(meta['git_commit'])}{dirty}")
    if payload["failed_runs"]:
        out.append(
            f"- Runs with a failed review call, excluded from scoring: {payload['failed_runs']}"
        )
    for note in payload.get("caveats", []):
        out.append(f"- **Caveat:** {note}")
    out.append("")
    s = payload["correctness_across_runs"]
    out.append("## Correctness")
    out.append("")
    out.append("| Metric | Mean | Std dev | Min | Max | Runs defined | Pooled |")
    out.append("|---|---|---|---|---|---|---|")
    pooled = s["pooled"]
    for name, label in (
        ("precision", "Precision"),
        ("recall", "Recall"),
        ("f1", "F1"),
        ("severity_weighted_recall", "Severity-weighted recall"),
    ):
        m = s[name]
        out.append(
            f"| {label} | {_fmt(m['mean'])} | {_fmt(m['stdev'])} | {_fmt(m['min'])} | "
            f"{_fmt(m['max'])} | {m['n_defined']} of {s['runs']} | {_fmt(pooled.get(name))} |"
        )
    out.append("")
    out.append("Pooled rates are computed once from the counts summed over all scored runs.")
    ref = payload["reference_run"]
    if ref is None:
        out.append("")
        out.append("No run completed, so there is nothing further to report.")
        return "\n".join(out) + "\n"
    c0 = payload["correctness_per_run"][ref]
    out.append("")
    out.append(
        f"Run {ref}: TP / FP / FN = {c0['true_positives']} / {c0['false_positives']} / "
        f"{c0['false_negatives']}; duplicate findings: {c0['duplicate_findings']}."
    )
    if c0["unmatched_expected"]:
        out.append("")
        out.append(f"Missed expected issues in run {ref}:")
        for ei in c0["unmatched_expected"]:
            out.append(f"- {ei['file']}:{ei['line']} ({ei['category']}, {ei['severity']})")
    if c0["negative_assertion_violations"]:
        out.append("")
        out.append(f"Negative-assertion violations in run {ref}:")
        for v in c0["negative_assertion_violations"]:
            out.append(
                f"- {v['file']} {v['category']} at line {v['comment_line']}: {v['rationale']}"
            )
    cc = payload["citation_checks_per_run"][ref]
    g = payload["grounding_summary_reference_run"]
    out.append("")
    out.append(f"## Citations (run {ref})")
    out.append("")
    out.append(
        f"- Comments citing at least one guideline: {_fmt(g['citation_rate'])} "
        f"(over {g['n_total_tasks']} comment(s))"
    )
    out.append(
        f"- Expected issues whose required guidelines were retrieved: "
        f"{_fmt(cc['must_cite_retrieved_rate'])} (over {cc['n_expected_with_must_cite']})"
    )
    out.append(
        f"- Matched issues whose comment cited the required guidelines: "
        f"{_fmt(cc['must_cite_cited_rate'])} (over {cc['n_matched_with_must_cite']})"
    )
    if "consistency" in payload:
        k = payload["consistency"]
        out.append("")
        out.append("## Consistency under repeat")
        out.append("")
        out.append("| Metric | Value |")
        out.append("|---|---|")
        out.append(f"| Runs scored / total | {k['runs_scored']} / {k['runs']} |")
        out.append(f"| Excluded (parse failures) | {k['excluded_parse_failures']} |")
        out.append(f"| Excluded (failed calls) | {k['excluded_call_failures']} |")
        out.append(f"| Mean pairwise Jaccard | {_fmt(k['mean_jaccard'])} |")
        out.append(f"| Min / max Jaccard | {_fmt(k['min_jaccard'])} / {_fmt(k['max_jaccard'])} |")
        out.append(f"| Severity stability | {_fmt(k['severity_stability'])} |")
    cuts = sorted(
        {c for run in payload["files_per_run"] for f in run for c in f["truncated_inputs"]}
    )
    errors = sum(1 for run in payload["files_per_run"] for f in run if f["parse_error"])
    models = sorted(
        {
            f["response_model"]
            for run in payload["files_per_run"]
            for f in run
            if f["response_model"]
        }
    )
    out.append("")
    out.append("## Input handling")
    out.append("")
    out.append(f"- Truncated inputs: {', '.join(cuts) if cuts else 'none'}")
    out.append(f"- Files with parse errors across runs: {errors}")
    out.append(f"- Model reported by the API: {', '.join(models) if models else 'n/a'}")
    return "\n".join(out) + "\n"


def write_reports(report_dir: Path, payload: dict, meta: dict) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    fid = payload["fixture_id"]  # validated by load_fixture as file-name safe
    (report_dir / f"{fid}.json").write_text(json.dumps({"run_metadata": meta, **payload}, indent=2))
    (report_dir / f"{fid}.md").write_text(_render_markdown(payload, meta))


def _write_summary(report_dir: Path, rows: list, meta: dict) -> None:
    out = ["# Evaluation summary", ""]
    out.append(f"- Mode: {meta['mode']}")
    out.append(f"- RAG: {'on' if meta['rag_enabled'] else 'OFF (ablation)'}")
    out.append(f"- Repeats per fixture: {meta['repeats']}")
    dirty = " (with uncommitted changes)" if meta.get("git_uncommitted_changes") else ""
    out.append(f"- Commit: {_fmt(meta['git_commit'])}{dirty}")
    for note in caveats(meta):
        out.append(f"- **Caveat:** {note}")
    out.append("")
    out.append(
        "| Fixture | Precision (mean) | Recall (mean) | F1 (mean) | Severity-weighted recall (mean) |"
    )
    out.append("|---|---|---|---|---|")
    for fid, s in rows:
        out.append(
            f"| {fid} | {_fmt(s['precision']['mean'])} | {_fmt(s['recall']['mean'])} | "
            f"{_fmt(s['f1']['mean'])} | {_fmt(s['severity_weighted_recall']['mean'])} |"
        )
    (report_dir / "summary.md").write_text("\n".join(out) + "\n")


def missing_live_credentials() -> list[str]:
    return [name for name in REQUIRED_LIVE_ENV if not os.getenv(name)]


async def amain(args: argparse.Namespace) -> int:
    if args.fixture:
        fixtures = [load_fixture(args.fixture)]
    else:
        fixtures = load_fixture_directory(args.fixtures)
    if not fixtures:
        print("No fixtures loaded", file=sys.stderr)
        return 1

    if not args.mock_llm:
        missing = missing_live_credentials()
        if missing:
            print(
                "Missing Azure OpenAI settings: " + ", ".join(missing) + ".\n"
                "Set them in the environment or in .env (see .env.example), "
                "or run offline with --mock-llm.",
                file=sys.stderr,
            )
            return 2

    engine = make_engine(rag_enabled=not args.no_rag, mock=args.mock_llm)
    report_dir = Path(args.report)
    git = git_state()  # before any report is written
    rows = []
    meta: dict = {}
    failed_runs = 0
    for fixture in fixtures:
        print(f"== {fixture.fixture_id} ==")
        runs = await run_repeats(fixture, engine, args.repeats)
        meta = run_metadata(args, engine, git)
        payload = score_fixture(fixture, runs, args.line_tolerance, meta)
        write_reports(report_dir, payload, meta)
        rows.append((fixture.fixture_id, payload["correctness_across_runs"]))
        failed_runs += len(payload["failed_runs"])

    _write_summary(report_dir, rows, meta)
    print(f"Reports written to {report_dir}/")
    if failed_runs:
        print(
            f"{failed_runs} run(s) had a failed review call; they are listed under "
            "failed_runs in the reports and excluded from scoring.",
            file=sys.stderr,
        )
        return 3
    return 0


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def main(argv: list[str] | None = None) -> int:
    # A .env in the working directory first, then one found upward from this
    # package (the repository root). Neither overrides variables already set.
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Evaluation harness for the Automated Code Review Agent."
    )
    fixture_group = parser.add_mutually_exclusive_group(required=True)
    fixture_group.add_argument("--fixture", type=Path, help="Run a single fixture file.")
    fixture_group.add_argument(
        "--fixtures", type=Path, help="Run every JSON fixture in this directory."
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
        help="RAG-off arm: retrieve no guidelines and put none in the prompt.",
    )
    parser.add_argument(
        "--repeats",
        type=_positive_int,
        default=1,
        help="Number of runs per fixture (consistency metrics need at least 2).",
    )
    parser.add_argument(
        "--line-tolerance",
        type=_non_negative_int,
        default=DEFAULT_LINE_TOLERANCE,
        help="Line window for matching findings to expected issues and across runs.",
    )
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="Run offline with a deterministic scanner-echo stand-in for the LLM. "
        "Checks the harness; does not produce model results.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
