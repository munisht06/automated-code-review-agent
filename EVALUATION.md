# Evaluation

This document defines how the Automated Code Review Agent is being measured. It is the methodological complement to [`RESEARCH.md`](./RESEARCH.md): where that document explains *what* is being studied, this one explains *how* the answers will be obtained, and against what evidence.

The evaluation framework is in active development. The harness skeleton lives in [`code_review_agent/evaluation/`](./code_review_agent/evaluation/) and is runnable today; the benchmark dataset is being built by hand and is not yet large enough to support strong empirical claims. Numerical results are explicitly omitted from this document until the dataset is at a defensible size — the methodology is published first so that the results, once they exist, can be assessed against a methodology that is fixed in writing.

## What is being measured

Four properties, aligned with the three working hypotheses in the README:

### 1. Correctness on labeled fixtures

For each fixture (a PR diff with one or more known issues, plus optional clean baselines), the harness runs the agent end-to-end and computes:

- **Recall** — fraction of injected issues that the agent surfaces (a fixture-injected SQL-injection vulnerability that the agent flags counts as a hit).
- **Precision** — fraction of agent findings that correspond to a real fixture-labeled issue.
- **F1** — harmonic mean of precision and recall.
- **Severity-weighted recall** — recall weighted by the severity of the missed issue. A missed `CRITICAL` security issue costs more than a missed `SUGGESTION`-level style nit.

Findings are matched to fixture-labeled issues by `(file, line, category)` triples with a configurable line-tolerance window (default ±3 lines) to avoid penalizing the agent for off-by-one disagreements with the fixture.

### 2. Grounding fidelity

When the agent's output cites a guideline (or a static-scanner finding), does the citation actually apply? Three sub-metrics:

- **Citation rate** — fraction of LLM-generated comments that cite *some* retrieved guideline or scanner finding.
- **Citation applicability** — on a hand-labeled subset, fraction of citations that human review judges to actually apply to the code being commented on.
- **Citation specificity** — when multiple guidelines are retrieved, does the cited one correspond to the most relevant retrieved chunk, or does the agent cite arbitrarily?

Grounding fidelity is the single hardest property to measure cheaply, because applicability requires human judgment. The harness produces a grounding-fidelity report that is structured as a labeling task: pairs of `(comment, cited guideline)` are emitted for human review, and the human-graded outputs are folded back into the metric.

### 3. RAG ablation

For each fixture, the harness can run the agent in three configurations:

- **Full system** — RAG retrieval + scanner + LLM.
- **No RAG** — scanner + LLM, with the system prompt stripped of retrieved guidelines.
- **No scanner** — RAG retrieval + LLM, with the static-scanner findings withheld from the prompt.

Cross-configuration comparison gives a direct read on how much of the system's correctness depends on each component.

### 4. Consistency under repeat

The same fixture run *N* times against the same model deployment with the same prompt produces *N* sets of findings. Variance metrics:

- **Set Jaccard** between findings on consecutive runs.
- **Comment-text edit distance** for findings that match by location across runs.
- **Severity-class stability** — fraction of repeat runs where the same finding is assigned the same severity.

Variance under repeat is a concrete proxy for the system's *reliability* claim. Temperature is pinned at `0.1` to bias toward reproducibility, but stochasticity remains; this is studyable rather than eliminable.

## Fixture format

Fixtures live under `tests/fixtures/prs/` and are JSON files with the following schema (a sample is bundled with the repo):

```json
{
  "fixture_id": "py-sql-injection-001",
  "language": "python",
  "description": "PR introduces an f-string SQL query in a user-lookup function",
  "files": [
    {
      "path": "src/users.py",
      "language": "python",
      "patch": "<unified diff>",
      "content": "<full file content at PR head>"
    }
  ],
  "expected_issues": [
    {
      "file": "src/users.py",
      "line": 17,
      "category": "security",
      "severity": "CRITICAL",
      "issue_type": "sql_injection",
      "must_cite": ["security_best_practices"]
    }
  ],
  "negative_assertions": [
    {
      "file": "src/users.py",
      "category": "style",
      "rationale": "The patch does not introduce style issues; flagging style here would be a false positive."
    }
  ]
}
```

The schema is enforced by `code_review_agent.evaluation.fixtures.load_fixture`. `expected_issues` are positive assertions ("the agent should surface this"); `negative_assertions` are negative ("the agent should not flag this category in this file").

## How to run the harness

```bash
# Run the full suite against all fixtures under tests/fixtures/prs/
python -m code_review_agent.evaluation.runner \
    --fixtures tests/fixtures/prs \
    --report reports/

# Run a single fixture
python -m code_review_agent.evaluation.runner \
    --fixture tests/fixtures/prs/py-sql-injection-001.json \
    --report reports/

# RAG ablation
python -m code_review_agent.evaluation.runner \
    --fixtures tests/fixtures/prs \
    --no-rag \
    --report reports/no-rag/

# Consistency under repeat (N=10)
python -m code_review_agent.evaluation.runner \
    --fixtures tests/fixtures/prs \
    --repeats 10 \
    --report reports/repeat/
```

Reports are written as JSON for downstream analysis and as a human-readable Markdown summary.

## What "passing" looks like

The harness does not have a pass/fail gate today. As the dataset matures, the intent is to add regression checks of the form:

- *Recall on the security-injection fixture set must be ≥ 0.85.*
- *Precision on the clean-code fixture set must be ≥ 0.90.*
- *Set Jaccard on the consistency suite at N=10 must be ≥ 0.75.*

These thresholds will be wired into CI once the fixture set is large enough that the thresholds are meaningful rather than noise.

## Current status

| Stream | Status |
|---|---|
| Harness skeleton (`runner.py`, `metrics.py`, `fixtures.py`) | In repo, runnable end-to-end |
| Sample fixture | One bundled (`tests/fixtures/prs/py-sql-injection-001.json`) — illustrative, not yet a benchmark |
| Correctness metrics | Implemented |
| Consistency metrics | Implemented |
| RAG ablation switch | Implemented |
| Grounding-fidelity scoring | Stubbed; emits labeling tasks rather than scores until human-graded data exists |
| Fixture set | ~1 fixture; building toward 30–50 across Python, TypeScript, and Go |
| Numerical results | Deliberately not reported until fixture set is at sufficient size |

## Known issues affecting evaluation today

- **`position` field in `create_pr_review`.** The orchestrator currently passes the source-file line number as the `position` field on inline review comments. GitHub's review API expects a diff position. For programmatic evaluation against fixtures, this does not affect the metrics — the harness operates on the structured `FileReviewResult` before the GitHub-API formatting step — but it does mean live-PR experiments cannot yet be used as evaluation evidence reliably. Tracked as a Phase-1 fix.
- **Single-language bias.** The bundled sample fixture is Python. Until the fixture set diversifies, claims about cross-language behavior should not be drawn.
- **Single embedding/model deployment.** All experiments to date are on a single Azure OpenAI configuration. Cross-provider variance is not yet measurable.

## Methodology change log

This section will track methodology changes as the framework evolves. Until results are reported, methodology is fluid; once results are in, methodology changes will be versioned and old results will be re-run against the new methodology when feasible.

| Date | Change | Rationale |
|---|---|---|
| 2026-05 | Initial methodology published | First written-down version of the evaluation framework |
