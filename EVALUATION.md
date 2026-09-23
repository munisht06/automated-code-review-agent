# Evaluation

This document defines how the Automated Code Review Agent is to be measured. It is the methodological complement to [`RESEARCH.md`](./RESEARCH.md): where that document explains *what* is being studied, this one explains *how* the answers will be obtained, and against what evidence.

The harness lives in [`code_review_agent/evaluation/`](./code_review_agent/evaluation/). It runs end to end offline with `--mock-llm`, which the tests exercise; the live path uses the same code, and no live run is committed. The benchmark dataset is being built by hand and currently holds one illustrative fixture. No results are reported. The methodology is written down before any results exist, and every change to it is logged below with its date and reason.

## What is being measured

The design has one noise floor, one intervention and two outcome measures. **Consistency under repeat** (§4) is the noise floor: with a stochastic model, a difference between two configurations means nothing until it is larger than the variation between repeat runs of a single configuration, so consistency is to be measured first. The **RAG ablation** (§3) is the intervention. **Correctness** (§1) and **grounding fidelity** (§2) are the outcome measures.

### 1. Correctness on labeled fixtures

For each fixture (a PR diff with known issues, or a clean baseline), the harness runs the review engine on every file and scores the model's line comments:

- **Recall**: fraction of expected issues matched by a finding.
- **Precision**: fraction of findings that match an expected issue.
- **F1**: 2TP / (2TP + FP + FN), the harmonic mean of precision and recall. A run that reports nothing against expected issues scores 0.
- **Severity-weighted recall**: recall weighted by the severity of each expected issue (CRITICAL 5, HIGH 4, WARNING or MEDIUM 2, SUGGESTION 1; any other string weights 1, and fixture severities are not validated), so a missed `CRITICAL` issue costs more than a missed style nit.

A finding matches an expected issue only when its category agrees with the label, so a comment that finds the issue but calls it `style` rather than `bug` is not credited: the fixtures test classification as well as detection. A finding matches an expected issue when the file and category agree (case-insensitive) and the lines are within a tolerance window (default ±3, set with `--line-tolerance`). Matching is one-to-one: it pairs as many findings with expected issues as possible, and among such pairings takes the one with the smallest total line distance. Each expected issue can be credited to one finding; a finding eligible only for issues that other findings took counts as a false positive and is reported as a duplicate. Remaining ties are broken in favor of a finding that cites the issue's `must_cite` guidelines, and findings are sorted by content before matching, so no score depends on the order in which the model listed them. A value that is undefined (precision when the agent reports nothing, recall on a fixture with no expected issues) is reported as n/a rather than 0, because a 0 would read as a measured failure.

Every false positive is also checked against the fixture's negative assertions, and violations are listed per run in the JSON report. With `--repeats N`, each rate is reported per run and summarized two ways: as mean, standard deviation, minimum and maximum over the runs where it is defined (with the number of those runs), and as a pooled rate computed once from the counts summed over all scored runs.

A run in which a review call failed (a network or API error, not a model output) is listed in the report and excluded from every score, and the harness exits with status 3.

A run whose model output failed to parse counts as a run with no findings. Only the model's comments are scored. The static scanner's findings are placed in the prompt but are not counted as agent findings unless the model restates them.

### 2. Grounding fidelity

When a comment cites a guideline, does the guideline apply? Guidelines are identified in the prompt by ID, and the model is asked to list the IDs each comment relies on in `cited_guideline_ids`. Citations are self-reported by the model; scanner findings are not counted as citations.

- **Citation rate**: fraction of the model's comments whose `cited_guideline_ids` is non-empty. Structural; needs no labels. The IDs are not checked against the retrieved set, so an invented ID counts. Reported for the reference run (the first run whose review calls all succeeded).
- **Required-citation checks**: a fixture's expected issue can declare `must_cite` guideline IDs. The harness reports the fraction of such issues whose required guidelines were all retrieved for that file, and, of those matched by a finding, the fraction whose matching comment cited them all. The two rates separate a guideline that never reached the prompt (a retrieval failure) from one that reached the prompt but was not applied (a grounding failure). The cited rate covers every matched issue, including ones whose guideline was not retrieved, so it isolates grounding failures only when the retrieved rate is 1.0.
- **Citation applicability**: on a hand-labeled subset, fraction of citations that a human judges to apply to the code being commented on.
- **Citation specificity**: when several guidelines are retrieved, whether the cited one is the most relevant of them.

Applicability and specificity need human judgment, so the harness emits them as a labeling task: one task per comment from the reference run, carrying the comment, the IDs it cites and the IDs retrieved for its file. `aggregate_grounding_labels` folds the graded tasks back into scores; until a batch is labeled, both scores are n/a.

### 3. RAG ablation

For each fixture, the agent is run in three configurations. The first two are implemented (`--no-rag` switches retrieval off); the third is planned.

- **Full system**: retrieved guidelines + scanner findings + LLM.
- **No RAG**: scanner findings + LLM; no guidelines are retrieved and the system prompt says none are provided.
- **No scanner** *(planned, not yet a runner flag)*: retrieved guidelines + LLM, with scanner findings withheld from the prompt.

Each configuration is one harness invocation, and the ablation compares their reports.

**Known confounds in the current setup.** These limit what the ablation can show today and are listed here so that no result is read past them:

- **Retrieval is not selective yet.** `top_k` is 3 and the bundled corpus holds two guidelines, so the full system receives every guideline for every file. The comparison is all guidelines against none, not selective retrieval against none, and the required-guideline retrieved rate is 1.0 by construction whenever RAG is on.
- **The scanner is in both arms.** `py-sql-injection-001`'s SQL injection is flagged by the scanner and placed in the prompt with RAG on or off, so that fixture cannot show a retrieval effect on detection. `py-silent-except-002` exists for this reason: its bare `except:` matches no scanner pattern, so what the agent says about it depends on the model and the prompt alone. Two fixtures are still far too few to conclude anything.
- **The corpus is generic.** The bundled guidelines are general best-practice notes that a large model largely knows already. The hypothesis concerns team-specific conventions, which require a team-authored corpus.
- **The prompt primes security in both arms.** The system prompt's focus list names SQL injection, XSS and hardcoded secrets whether or not guidelines are present.
- **Mock mode is plumbing only.** Under `--mock-llm` the stand-in echoes the scanner findings and ignores the guidelines, so it behaves like a scanner-only configuration: among the scores, both arms differ only in the required-guideline retrieved rate (1.0 with RAG, 0 without). It checks that the harness runs; it measures nothing about the model, and every report says so in its own caveats, beside the numbers.

### 4. Consistency under repeat

The same fixture run *N* times against the same deployment with the same prompt produces *N* sets of findings. A finding is a (file, category, line, severity) tuple, and two findings in different runs are the same finding when the file and category agree and the lines are within the same tolerance used for correctness, matched one-to-one as in §1, with ties broken in favor of equal severities.

- **Pairwise Jaccard**: for every pair of runs, matched findings divided by the union (|A| + |B| − matched), averaged over all pairs and reported with its minimum and maximum. Two empty runs score 1.
- **Severity stability**: of the findings in the first scored run that are matched in every other run (each run matched to the first one-to-one), the fraction assigned the same severity in all of them; n/a when no finding persists across all runs.
- **Comment-text edit distance** *(planned)*: for findings matched across runs, how much the wording changes.

A run in which any file's output failed to parse (a recorded parse error and no comments), or any review call failed, is excluded from these metrics and counted, because such a run has no findings and two empty runs would otherwise score as perfectly consistent.

Temperature is pinned at `0.1` to bias toward reproducibility, but stochasticity remains; how much is what this section measures. The per-run correctness summary (§1) gives the same noise floor for the correctness rates.

## Fixture format

Fixtures live under `tests/fixtures/prs/` and are JSON files with the following schema (the bundled sample, abbreviated):

```json
{
  "fixture_id": "py-sql-injection-001",
  "language": "python",
  "description": "PR introduces an f-string SQL query in a user-lookup function...",
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
      "line": 10,
      "category": "security",
      "severity": "CRITICAL",
      "issue_type": "sql_injection",
      "must_cite": ["python_best_practices"]
    }
  ],
  "negative_assertions": [
    {
      "file": "src/users.py",
      "category": "security",
      "line": 18,
      "line_tolerance": 3,
      "rationale": "get_user_safe is the parameterized version and is not vulnerable."
    }
  ]
}
```

`expected_issues` are positive assertions ("the agent should surface this"); `negative_assertions` are negative ("the agent should not flag this category here"). A negative assertion with a `line` is line-scoped: it covers only lines within its `line_tolerance` (default 3). Without a `line` it covers the whole file. Line scoping lets one file hold a vulnerable function and its safe counterpart: in the sample, the SQL injection at line 10 is expected, and a security finding within 3 lines of line 18 (the body of `get_user_safe`, the parameterized version) is a violation.

Fixtures are loaded by `code_review_agent.evaluation.fixtures.load_fixture`, which raises on a missing required field and on a `fixture_id` that is not safe as a file name or is the reserved name `summary` (report files are named after it); loading a directory also rejects duplicate IDs. It does not validate other values, such as category names.

## How to run the harness

```bash
# Offline, no credentials: exercises the whole harness with a stand-in for
# the LLM. Its numbers are not model results.
python -m code_review_agent.evaluation.runner \
    --fixtures tests/fixtures/prs \
    --mock-llm \
    --report reports/mock/

# Run the suite against Azure OpenAI (credentials in the environment or .env)
python -m code_review_agent.evaluation.runner \
    --fixtures tests/fixtures/prs \
    --report reports/

# Run a single fixture
python -m code_review_agent.evaluation.runner \
    --fixture tests/fixtures/prs/py-sql-injection-001.json \
    --report reports/

# RAG ablation (compare with the full-system report)
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

The runner reads `.env` from the working directory (or the nearest parent that has one) and from the repository root; where both set a variable the working-directory file wins, and variables already in the environment are never overridden. A live run without Azure credentials stops before calling anything, names the missing variables, and exits with status 2 (argparse also uses 2 for invalid arguments; no fixtures found is 1). A run in which any review call failed still writes its reports and exits with status 3.

Each run writes a JSON report and a Markdown summary per fixture, plus `summary.md` across fixtures. Each report also carries its own caveats: whether it came from the offline stand-in, whether `top_k` covers the whole corpus (which makes the required-guideline retrieved rate 1.0 by construction), and whether RAG was off. The JSON report records what produced it (timestamp, git commit and whether the package, guidelines or fixtures differ from it, mode, chat and embedding deployment names, the embedding model name the API reported, API version, temperature, RAG on or off, repeats, line tolerance, top-k, language boost, prompt budgets, corpus source and guideline IDs, Python version) and, for every run and file, the retrieved guideline IDs, any truncated inputs, parse and call errors, the model name the API reported, and the raw model response.

## What "passing" looks like

The harness has no pass/fail gate today. Once the fixture set is large enough, the intent is to add regression checks of this form, with thresholds set from the measured run-to-run variation rather than chosen in advance:

- *Recall on the security-injection fixtures at or above a threshold.*
- *False positives on the clean-code fixtures at or below a threshold.* (A clean fixture has no expected issues, so its precision is either undefined or 0; false-positive counts are the meaningful measure there.)
- *Mean pairwise Jaccard on the consistency suite at N=10 at or above a threshold.*

These checks will be wired into CI once the thresholds would be meaningful rather than noise.

## Current status

| Stream | Status |
|---|---|
| Harness (`runner.py`, `metrics.py`, `fixtures.py`) | Runs end to end offline with `--mock-llm` (tested); the live path uses the same code, and no live run is committed |
| Offline tests | Metrics, parsing, prompt construction, scanner guards, diff-line mapping, webhook handling and the review loop (with stubbed GitHub and engine objects) are tested without credentials |
| Bundled fixtures | Two: `py-sql-injection-001` (scanner-detectable) and `py-silent-except-002` (invisible to the scanner). Illustrative, not a benchmark. |
| Correctness metrics | Implemented, with maximum one-to-one matching, duplicate counting, negative assertions, and per-run, mean and pooled summaries |
| Consistency metrics | Pairwise Jaccard and severity stability implemented, with parse-failure exclusion; edit distance planned |
| RAG ablation switch | Implemented (`--no-rag`); scanner-off switch planned |
| Citation IDs in model output | Requested in the JSON structure the prompt asks for (`cited_guideline_ids`) and parsed; self-reported by the model |
| Required-citation checks | Implemented (`must_cite` retrieved and cited rates) |
| Grounding-fidelity labeling | Harness emits labeling tasks with cited and retrieved IDs; `aggregate_grounding_labels` scores them. No labeled batch exists yet, so applicability and specificity are n/a. |
| Fixture set | Two fixtures; building toward 30–50 across Python, TypeScript and Go, including clean fixtures and more issues the scanner cannot detect |
| Numerical results | None reported |

## Known issues affecting evaluation today

- **Ablation confounds.** See §3: with the bundled corpus and fixture, the ablation cannot yet isolate an effect of selective retrieval.
- **Single-language bias.** Both bundled fixtures are Python. Until the fixture set diversifies, no claims about cross-language behavior should be drawn.
- **Single model configuration.** The harness targets one Azure OpenAI configuration; cross-model and cross-provider variance is not yet measurable.
- **Self-reported citations.** `cited_guideline_ids` is what the model says it used. Whether a citation applies is established only by the labeling step.

## Methodology change log

Until results are reported, methodology is fluid; once results exist, methodology changes will be versioned and old results re-run against the new methodology when feasible.

| Date | Change | Rationale |
|---|---|---|
| 2026-05 | Initial methodology written down | First written-down version of the evaluation framework |
| 2026-05-07 | Scanner findings in the prompt sorted by severity before the existing five-finding cap, which had used catalog order and could withhold a CRITICAL finding; line-scoped negative assertions; negative-assertion violations recorded in correctness reports; grounding-label aggregator added | Prompt budget spent on the most severe findings; fixtures can contain a vulnerable and a safe example side by side; labels can be folded back into scores |
| 2026-09-22 | Per-guideline prompt budget raised from 500 to 4,000 characters; fixture `must_cite` pointed at the guideline ID that loads | The 500-character cap cut the Security section from every bundled guideline |
| 2026-09-22 | Maximum one-to-one matching with duplicate counting; F1 as 2TP / (2TP + FP + FN); n/a for undefined rates, with defined-run counts and pooled rates; runs with a failed review call excluded from scoring; tie-breaks that make scores independent of the order of findings; fixture IDs validated (file-name safe, `summary` reserved, duplicates rejected); reports that carry their own caveats; a second fixture whose issue the scanner cannot detect; tolerance-matched consistency with parse-failure exclusion; guideline IDs in the prompt and `cited_guideline_ids` in the output; `must_cite` checks; visible truncation markers and line-numbered file content; language boost fixed; fixture patch regenerated so its hunk matches the file; run metadata and per-file provenance in reports; offline mock mode | Earlier matching let two comments on one issue both count as hits, and averaging F1 only over runs with a defined precision overstated it; consistency required exact line matches, so a one-line shift counted as two different findings, and unparsed runs scored as consistent; citation scoring had no IDs to score; the language boost never applied |
