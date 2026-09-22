"""
Metrics for the evaluation harness.

Four metric families mirror the methodology in EVALUATION.md:

    correctness  -- precision, recall, F1, severity-weighted recall against
                    fixture-labeled expected issues, with one-to-one matching.
    consistency  -- pairwise set Jaccard and severity stability across N
                    repeat runs of the same fixture, using the same line
                    tolerance as correctness.
    citation     -- structural checks against each expected issue's
                    ``must_cite`` list: was the guideline retrieved, and did
                    the matching comment cite it?
    grounding    -- emits (comment, cited and retrieved guideline IDs) as
                    labeling tasks for human grading; aggregates labels into
                    citation applicability and specificity scores.

Metrics operate over the agent's typed FileReviewResult objects and the
fixture's ExpectedIssue / NegativeAssertion dataclasses; they do NOT depend
on the live runtime path (no GitHub API, no Azure OpenAI).

Undefined quantities are reported as ``None`` rather than 0: precision with
no findings, recall with no expected issues, F1 with neither, severity
stability with no finding present in every run. A 0 would read as a measured
failure.

Matching (of findings to expected issues, and of findings across repeat
runs) is one-to-one and maximum: it pairs as many findings as possible, and
among pairings of that size it takes the one with the smallest total line
distance. A greedy closest-first pass is not enough: with expected issues at
lines 10 and 14 and findings at 8 and 11, greedy pairs 11 with 10 and leaves
8 unmatched, although 8-10 and 11-14 match both.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

from code_review_agent.review_engine import FileReviewResult, LineComment

from .fixtures import ExpectedIssue, Fixture, NegativeAssertion

# ---- correctness ------------------------------------------------------------


# Severity weight used by severity-weighted recall.
# Tunable; numbers chosen so a missed CRITICAL costs ~5x a missed SUGGESTION.
_SEVERITY_WEIGHTS = {
    "CRITICAL": 5.0,
    "HIGH": 4.0,
    "WARNING": 2.0,
    "MEDIUM": 2.0,
    "SUGGESTION": 1.0,
}

# Default tolerance window for matching agent comments to expected issues,
# and for matching findings across repeat runs.
DEFAULT_LINE_TOLERANCE = 3


def _norm(category: str) -> str:
    return (category or "").strip().lower()


@dataclass
class NegativeAssertionViolation:
    """A finding that violated a labeled negative assertion."""

    comment: LineComment
    assertion: NegativeAssertion


@dataclass
class CorrectnessMetrics:
    """Correctness metrics for one fixture run.

    Matching is one-to-one: each expected issue can be credited to at most
    one finding. Further findings on an already-matched issue are counted as
    false positives and also reported as ``duplicate_findings``.
    """

    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    duplicate_findings: int = 0
    severity_weighted_recall_numerator: float = 0.0
    severity_weighted_recall_denominator: float = 0.0
    matched_pairs: list[tuple] = field(default_factory=list)
    unmatched_expected: list[ExpectedIssue] = field(default_factory=list)
    unmatched_findings: list[LineComment] = field(default_factory=list)
    negative_assertion_violations: list[NegativeAssertionViolation] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else None

    @property
    def f1(self) -> float | None:
        # 2TP / (2TP + FP + FN): defined whenever there is anything to score,
        # so a run that reports nothing against expected issues scores 0
        # rather than dropping out of the average.
        denom = 2 * self.true_positives + self.false_positives + self.false_negatives
        return 2 * self.true_positives / denom if denom else None

    @property
    def severity_weighted_recall(self) -> float | None:
        if not self.severity_weighted_recall_denominator:
            return None
        return self.severity_weighted_recall_numerator / self.severity_weighted_recall_denominator

    def to_dict(self) -> dict:
        return {
            "precision": _round(self.precision),
            "recall": _round(self.recall),
            "f1": _round(self.f1),
            "severity_weighted_recall": _round(self.severity_weighted_recall),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "duplicate_findings": self.duplicate_findings,
            "negative_assertion_violations": [
                {
                    "file": v.assertion.file,
                    "category": v.assertion.category,
                    "line": v.assertion.line,
                    "comment_line": v.comment.line,
                    "rationale": v.assertion.rationale,
                }
                for v in self.negative_assertion_violations
            ],
            "unmatched_expected": [vars(e) for e in self.unmatched_expected],
        }


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def _min_cost_assignment(cost: list[list[float]]) -> list[tuple[int, int]]:
    """Hungarian algorithm for a rectangular cost matrix with rows <= columns.

    Returns (row, column) pairs assigning every row to a distinct column at
    minimum total cost.
    """
    n, m = len(cost), len(cost[0])
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)  # p[j]: row assigned to column j (1-based; 0 = none)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    return [(p[j] - 1, j - 1) for j in range(1, m + 1) if p[j]]


def _max_matching(edges: dict[tuple[int, int], int]) -> list[tuple[int, int]]:
    """Maximum one-to-one matching over candidate pairs, minimum total distance
    among maximum matchings.

    ``edges`` maps (left index, right index) to a non-negative distance; only
    listed pairs may be matched. Non-edges get a cost larger than any possible
    sum of real distances, so the assignment first maximizes the number of
    real pairs and then minimizes their total distance.
    """
    if not edges:
        return []
    left = sorted({i for i, _ in edges})
    right = sorted({j for _, j in edges})
    transpose = len(left) > len(right)
    rows, cols = (right, left) if transpose else (left, right)
    big = (max(edges.values()) + 1) * (len(rows) + 1)

    def key(r: int, c: int) -> tuple[int, int]:
        return (c, r) if transpose else (r, c)

    cost = [[float(edges.get(key(r, c), big)) for c in cols] for r in rows]
    pairs = [key(rows[ri], cols[ci]) for ri, ci in _min_cost_assignment(cost)]
    return sorted(pair for pair in pairs if pair in edges)


def compute_correctness(
    fixture: Fixture,
    results: Sequence[FileReviewResult],
    line_tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> CorrectnessMetrics:
    """
    Score a single fixture's agent output against its expected issues.

    A finding can match an expected issue iff they have the same file, the
    same category (case-insensitive), and line numbers within
    ``line_tolerance`` of each other. Matching is one-to-one and maximum (see
    the module docstring): each finding and each expected issue is used at
    most once.

    A finding that matches no expected issue is a false positive. If it was
    eligible for an issue that another finding already took, it is also
    counted in ``duplicate_findings``. Every false positive is checked
    against the fixture's negative assertions, matched by (file, category)
    and, for line-scoped assertions, by the assertion's line tolerance.
    """
    metrics = CorrectnessMetrics()

    findings: list[tuple[str, LineComment]] = [
        (result.filename, comment) for result in results for comment in result.line_comments
    ]

    edges: dict[tuple[int, int], int] = {}
    for fi, (fname, comment) in enumerate(findings):
        for ei_idx, ei in enumerate(fixture.expected_issues):
            if (
                ei.file == fname
                and _norm(ei.category) == _norm(comment.category)
                and abs(ei.line - comment.line) <= line_tolerance
            ):
                edges[(fi, ei_idx)] = abs(ei.line - comment.line)

    finding_to_expected = dict(_max_matching(edges))
    matched_expected = set(finding_to_expected.values())
    eligible_findings = {fi for fi, _ in edges}

    negatives_by_key: dict[tuple[str, str], list[NegativeAssertion]] = {}
    for na in fixture.negative_assertions:
        negatives_by_key.setdefault((na.file, _norm(na.category)), []).append(na)

    for fi, (fname, comment) in enumerate(findings):
        if fi in finding_to_expected:
            metrics.true_positives += 1
            metrics.matched_pairs.append(
                (comment, fixture.expected_issues[finding_to_expected[fi]])
            )
            continue

        metrics.false_positives += 1
        metrics.unmatched_findings.append(comment)
        if fi in eligible_findings:
            metrics.duplicate_findings += 1

        for na in negatives_by_key.get((fname, _norm(comment.category)), []):
            if na.line is None or abs(na.line - comment.line) <= na.line_tolerance:
                metrics.negative_assertion_violations.append(
                    NegativeAssertionViolation(comment=comment, assertion=na)
                )

    for idx, ei in enumerate(fixture.expected_issues):
        weight = _SEVERITY_WEIGHTS.get((ei.severity or "").upper(), 1.0)
        metrics.severity_weighted_recall_denominator += weight
        if idx in matched_expected:
            metrics.severity_weighted_recall_numerator += weight
        else:
            metrics.false_negatives += 1
            metrics.unmatched_expected.append(ei)

    return metrics


def summarize_correctness(per_run: Sequence[CorrectnessMetrics]) -> dict:
    """Summarize correctness across repeat runs.

    For each rate: mean, standard deviation, min and max over the runs where
    it is defined, with ``n_defined`` saying how many that is (precision, for
    one, is undefined in a run that reports nothing). ``pooled`` gives the
    rates computed once from the summed counts of all runs, which every run
    contributes to.
    """
    out: dict = {"runs": len(per_run)}
    for name in ("precision", "recall", "f1", "severity_weighted_recall"):
        values = [getattr(m, name) for m in per_run]
        defined = [v for v in values if v is not None]
        out[name] = {
            "mean": _round(statistics.fmean(defined)) if defined else None,
            "stdev": _round(statistics.stdev(defined)) if len(defined) > 1 else None,
            "min": _round(min(defined)) if defined else None,
            "max": _round(max(defined)) if defined else None,
            "n_defined": len(defined),
            "per_run": [_round(v) for v in values],
        }
    pooled = CorrectnessMetrics(
        true_positives=sum(m.true_positives for m in per_run),
        false_positives=sum(m.false_positives for m in per_run),
        false_negatives=sum(m.false_negatives for m in per_run),
        severity_weighted_recall_numerator=sum(
            m.severity_weighted_recall_numerator for m in per_run
        ),
        severity_weighted_recall_denominator=sum(
            m.severity_weighted_recall_denominator for m in per_run
        ),
    )
    out["pooled"] = {
        "true_positives": pooled.true_positives,
        "false_positives": pooled.false_positives,
        "false_negatives": pooled.false_negatives,
        "precision": _round(pooled.precision),
        "recall": _round(pooled.recall),
        "f1": _round(pooled.f1),
        "severity_weighted_recall": _round(pooled.severity_weighted_recall),
    }
    return out


# ---- consistency ------------------------------------------------------------


@dataclass
class ConsistencyMetrics:
    """Variance metrics across N repeat runs of the same fixture.

    Runs are excluded, and counted, when a file's LLM output failed to parse
    (``excluded_parse_failures``) or its review call failed
    (``excluded_call_failures``): such a run has no findings, and two empty
    runs would otherwise score as perfectly consistent.
    """

    runs: int = 0
    runs_scored: int = 0
    excluded_parse_failures: int = 0
    excluded_call_failures: int = 0
    line_tolerance: int = DEFAULT_LINE_TOLERANCE
    pairwise_jaccard: list[float] = field(default_factory=list)
    severity_stability: float | None = None

    @property
    def mean_jaccard(self) -> float | None:
        if not self.pairwise_jaccard:
            return None
        return sum(self.pairwise_jaccard) / len(self.pairwise_jaccard)

    def to_dict(self) -> dict:
        return {
            "runs": self.runs,
            "runs_scored": self.runs_scored,
            "excluded_parse_failures": self.excluded_parse_failures,
            "excluded_call_failures": self.excluded_call_failures,
            "line_tolerance": self.line_tolerance,
            "mean_jaccard": _round(self.mean_jaccard),
            "min_jaccard": _round(min(self.pairwise_jaccard)) if self.pairwise_jaccard else None,
            "max_jaccard": _round(max(self.pairwise_jaccard)) if self.pairwise_jaccard else None,
            "severity_stability": _round(self.severity_stability),
        }


def compute_consistency(
    runs: Sequence[Sequence[FileReviewResult]],
    line_tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> ConsistencyMetrics:
    """
    Score variance across N repeat agent runs over the same fixture input.

    ``runs`` is a sequence of run-outputs; each run-output is a sequence of
    FileReviewResult objects. Findings are (file, category, line) triples and
    two findings in different runs are the same finding if file and category
    agree and lines are within ``line_tolerance``, the same rule correctness
    uses. Jaccard is averaged over all pairs of scored runs.
    """
    metrics = ConsistencyMetrics(runs=len(runs), line_tolerance=line_tolerance)
    scored = []
    for run in runs:
        if any(r.call_error for r in run):
            metrics.excluded_call_failures += 1
        elif any(r.parse_error and not r.line_comments for r in run):
            metrics.excluded_parse_failures += 1
        else:
            scored.append(run)
    metrics.runs_scored = len(scored)
    if len(scored) < 2:
        return metrics

    finding_lists = [_findings(run) for run in scored]
    for i in range(len(finding_lists)):
        for j in range(i + 1, len(finding_lists)):
            metrics.pairwise_jaccard.append(
                _tolerant_jaccard(finding_lists[i], finding_lists[j], line_tolerance)
            )

    metrics.severity_stability = _severity_stability(finding_lists, line_tolerance)
    return metrics


def _findings(run: Sequence[FileReviewResult]) -> list[tuple[str, str, int, str]]:
    return [
        (result.filename, _norm(c.category), c.line, (c.severity or "").upper())
        for result in run
        for c in result.line_comments
    ]


def _match(a: list, b: list, tol: int) -> list[tuple[int, int]]:
    """One-to-one maximum matching of findings by (file, category, |line| <= tol)."""
    edges: dict[tuple[int, int], int] = {}
    for i, (fa, ca, la, _sa) in enumerate(a):
        for j, (fb, cb, lb, _sb) in enumerate(b):
            if fa == fb and ca == cb and abs(la - lb) <= tol:
                edges[(i, j)] = abs(la - lb)
    return _max_matching(edges)


def _tolerant_jaccard(a: list, b: list, tol: int) -> float:
    if not a and not b:
        return 1.0
    m = len(_match(a, b, tol))
    return m / (len(a) + len(b) - m)


def _severity_stability(finding_lists: list, tol: int) -> float | None:
    """Of the findings in the first run that are matched in every other run,
    the fraction assigned the same severity in all of them. Each other run is
    matched to the first one-to-one, as in Jaccard. ``None`` if no finding
    persists across all runs."""
    base = finding_lists[0]
    others = finding_lists[1:]
    matches = [dict(_match(base, other, tol)) for other in others]
    persistent = 0
    stable = 0
    for i, finding in enumerate(base):
        if not all(i in m for m in matches):
            continue
        persistent += 1
        severities = {finding[3]} | {
            other[m[i]][3] for other, m in zip(others, matches, strict=True)
        }
        if len(severities) == 1:
            stable += 1
    return stable / persistent if persistent else None


# ---- citation checks against must_cite --------------------------------------


@dataclass
class CitationChecks:
    """Structural checks of each expected issue's ``must_cite`` guidelines.

    ``retrieved_rate``: over expected issues that declare ``must_cite``, the
    fraction whose required guidelines were all retrieved for that file.
    ``cited_rate``: over those expected issues that were matched by a
    finding, the fraction whose matching comment cited all of them.

    Together they separate two failures the grounding question cares about:
    a required guideline that never reached the prompt (a retrieval failure)
    and one that reached the prompt but was not applied (a grounding failure).
    """

    n_with_must_cite: int = 0
    n_retrieved: int = 0
    n_matched: int = 0
    n_cited: int = 0

    @property
    def retrieved_rate(self) -> float | None:
        return self.n_retrieved / self.n_with_must_cite if self.n_with_must_cite else None

    @property
    def cited_rate(self) -> float | None:
        return self.n_cited / self.n_matched if self.n_matched else None

    def to_dict(self) -> dict:
        return {
            "n_expected_with_must_cite": self.n_with_must_cite,
            "must_cite_retrieved_rate": _round(self.retrieved_rate),
            "n_matched_with_must_cite": self.n_matched,
            "must_cite_cited_rate": _round(self.cited_rate),
        }


def compute_citation_checks(
    fixture: Fixture,
    results: Sequence[FileReviewResult],
    correctness: CorrectnessMetrics,
) -> CitationChecks:
    """Check must_cite guidelines against retrieval and citations for one run."""
    checks = CitationChecks()
    retrieved_by_file = {r.filename: set(r.retrieved_guideline_ids) for r in results}
    comment_for_expected = {id(ei): comment for comment, ei in correctness.matched_pairs}

    for ei in fixture.expected_issues:
        if not ei.must_cite:
            continue
        required = set(ei.must_cite)
        checks.n_with_must_cite += 1
        if required <= retrieved_by_file.get(ei.file, set()):
            checks.n_retrieved += 1
        comment = comment_for_expected.get(id(ei))
        if comment is not None:
            checks.n_matched += 1
            if required <= set(comment.cited_guideline_ids):
                checks.n_cited += 1
    return checks


# ---- grounding fidelity (labeling-task emission) ----------------------------


@dataclass
class GroundingTask:
    """A pair surfaced for human grading: did the citation actually apply?"""

    fixture_id: str
    file: str
    line: int
    comment_text: str
    cited_guideline_ids: list[str]
    retrieved_guideline_ids: list[str]
    # Filled in by the human grader after the fact.
    applicable: bool | None = None
    specific: bool | None = None


def emit_grounding_tasks(
    fixture: Fixture,
    results: Sequence[FileReviewResult],
    retrieved_guideline_ids_per_file: dict[str, list[str]] | None = None,
) -> list[GroundingTask]:
    """
    Emit one labeling task per LLM comment, carrying the guideline IDs the
    comment cites and the IDs that were retrieved for its file.

    Retrieved IDs come from each FileReviewResult unless overridden by
    ``retrieved_guideline_ids_per_file``. A grader judges whether each cited
    guideline applies (``applicable``) and whether it is the most relevant of
    the retrieved ones (``specific``).
    """
    overrides = retrieved_guideline_ids_per_file or {}
    tasks: list[GroundingTask] = []
    for result in results:
        retrieved = overrides.get(result.filename, result.retrieved_guideline_ids)
        for c in result.line_comments:
            tasks.append(
                GroundingTask(
                    fixture_id=fixture.fixture_id,
                    file=result.filename,
                    line=c.line,
                    comment_text=f"[{c.severity}] {c.issue} -- {c.suggestion}",
                    cited_guideline_ids=list(c.cited_guideline_ids),
                    retrieved_guideline_ids=list(retrieved),
                )
            )
    return tasks


@dataclass
class GroundingFidelityMetrics:
    """Aggregate scores over a set of human-labeled GroundingTask objects.

    ``citation_rate`` is structural and available without labels.
    Applicability and specificity are only defined once a grader has filled
    in the corresponding fields; until then they are ``None``.
    """

    citation_applicability: float | None = None
    citation_specificity: float | None = None
    citation_rate: float | None = None
    n_total_tasks: int = 0
    n_labeled_applicable: int = 0
    n_labeled_specific: int = 0

    def to_dict(self) -> dict:
        return {
            "citation_applicability": _round(self.citation_applicability),
            "citation_specificity": _round(self.citation_specificity),
            "citation_rate": _round(self.citation_rate),
            "n_total_tasks": self.n_total_tasks,
            "n_labeled_applicable": self.n_labeled_applicable,
            "n_labeled_specific": self.n_labeled_specific,
        }


def aggregate_grounding_labels(tasks: Sequence[GroundingTask]) -> GroundingFidelityMetrics:
    """Aggregate grounding tasks into fidelity scores.

    - ``citation_rate``: fraction of tasks whose comment cited at least one
      guideline (structural; no labels needed).
    - ``citation_applicability``: of the tasks where ``applicable`` is set,
      the fraction labeled ``True``.
    - ``citation_specificity``: of the tasks where ``specific`` is set, the
      fraction labeled ``True``.
    """
    metrics = GroundingFidelityMetrics()
    if not tasks:
        return metrics

    metrics.n_total_tasks = len(tasks)
    metrics.citation_rate = sum(1 for t in tasks if t.cited_guideline_ids) / len(tasks)

    labeled_applicable = [t for t in tasks if t.applicable is not None]
    metrics.n_labeled_applicable = len(labeled_applicable)
    if labeled_applicable:
        metrics.citation_applicability = sum(1 for t in labeled_applicable if t.applicable) / len(
            labeled_applicable
        )

    labeled_specific = [t for t in tasks if t.specific is not None]
    metrics.n_labeled_specific = len(labeled_specific)
    if labeled_specific:
        metrics.citation_specificity = sum(1 for t in labeled_specific if t.specific) / len(
            labeled_specific
        )

    return metrics
