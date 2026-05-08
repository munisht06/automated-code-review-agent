"""
Metrics for the evaluation harness.

Three metric families mirror the methodology in EVALUATION.md:

    correctness  -- precision, recall, F1, severity-weighted recall against
                    fixture-labeled expected issues.
    grounding    -- emits (comment, cited_guideline) pairs as labeling tasks
                    for human grading; aggregates labels into citation
                    applicability and specificity scores.
    consistency  -- set Jaccard, comment-text edit distance, and severity-
                    class stability across N repeat runs of the same fixture.

Metrics operate over the agent's typed FileReviewResult objects and the
fixture's ExpectedIssue / NegativeAssertion dataclasses; they do NOT depend
on the live runtime path (no GitHub API, no Azure OpenAI). This decoupling
is what lets the harness be run reproducibly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

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

# Default tolerance window for matching agent comments to expected issues.
DEFAULT_LINE_TOLERANCE = 3


@dataclass
class NegativeAssertionViolation:
    """A finding that violated a labeled negative assertion."""

    comment: LineComment
    assertion: NegativeAssertion


@dataclass
class CorrectnessMetrics:
    """Aggregate correctness metrics over a fixture or fixture set."""

    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    severity_weighted_recall_numerator: float = 0.0
    severity_weighted_recall_denominator: float = 0.0
    matched_pairs: List[tuple] = field(default_factory=list)
    unmatched_expected: List[ExpectedIssue] = field(default_factory=list)
    unmatched_findings: List[LineComment] = field(default_factory=list)
    negative_assertion_violations: List[NegativeAssertionViolation] = field(
        default_factory=list
    )

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def severity_weighted_recall(self) -> float:
        return (
            self.severity_weighted_recall_numerator
            / self.severity_weighted_recall_denominator
            if self.severity_weighted_recall_denominator
            else 0.0
        )

    def to_dict(self) -> dict:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "severity_weighted_recall": round(self.severity_weighted_recall, 4),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
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


def compute_correctness(
    fixture: Fixture,
    results: Sequence[FileReviewResult],
    line_tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> CorrectnessMetrics:
    """
    Score a single fixture's agent output against its expected issues.

    Matching rule: an agent finding matches an expected issue iff
        - same file, AND
        - same category, AND
        - line numbers within ``line_tolerance`` of each other.

    A finding that matches no expected issue is counted as a false positive.
    Additionally, every false-positive finding is checked against the
    fixture's negative assertions; any finding that violates a negative
    assertion is recorded in ``negative_assertion_violations`` so reports
    can distinguish "the agent flagged something extra" from "the agent
    flagged something the fixture explicitly said it should not flag."

    Negative assertions are matched by ``(file, category)``; when an
    assertion has a ``line`` set, the violation must also be within the
    assertion's ``line_tolerance`` of that line. Line-scoping lets fixtures
    express intent like "do not flag the parameterized version on line 18"
    without forbidding all same-category findings in the file.
    """
    metrics = CorrectnessMetrics()

    # Index expected issues by file for fast lookup.
    expected_by_file: dict[str, list[ExpectedIssue]] = {}
    for ei in fixture.expected_issues:
        expected_by_file.setdefault(ei.file, []).append(ei)

    # Group negative assertions by (file, category); multiple line-scoped
    # assertions with the same category in the same file are allowed.
    negatives_by_key: dict[tuple[str, str], list[NegativeAssertion]] = {}
    for na in fixture.negative_assertions:
        negatives_by_key.setdefault((na.file, na.category), []).append(na)

    matched_expected: set[int] = set()

    for result in results:
        for comment in result.line_comments:
            best_match_idx = _find_best_match(
                comment, result.filename, expected_by_file, line_tolerance
            )
            if best_match_idx is not None:
                metrics.true_positives += 1
                matched_expected.add(best_match_idx)
                metrics.matched_pairs.append(
                    (comment, fixture.expected_issues[best_match_idx])
                )
                continue

            metrics.false_positives += 1
            metrics.unmatched_findings.append(comment)

            # Did this finding violate any negative assertion for the file +
            # category? File-scoped assertions match unconditionally; line-
            # scoped assertions match only within the assertion's tolerance.
            for na in negatives_by_key.get((result.filename, comment.category), []):
                if na.line is None or abs(na.line - comment.line) <= na.line_tolerance:
                    metrics.negative_assertion_violations.append(
                        NegativeAssertionViolation(comment=comment, assertion=na)
                    )

    # Anything in expected_issues not matched is a false negative.
    for idx, ei in enumerate(fixture.expected_issues):
        weight = _SEVERITY_WEIGHTS.get(ei.severity, 1.0)
        metrics.severity_weighted_recall_denominator += weight
        if idx in matched_expected:
            metrics.severity_weighted_recall_numerator += weight
        else:
            metrics.false_negatives += 1
            metrics.unmatched_expected.append(ei)

    return metrics


def _find_best_match(
    comment: LineComment,
    filename: str,
    expected_by_file: dict,
    line_tolerance: int,
) -> int | None:
    """Return the index of the best matching expected issue, or None."""
    # Find the nearest expected issue in the same file with the same category.
    candidates = [
        (i, ei)
        for i, ei in enumerate(expected_by_file.get(filename, []))
        if ei.category == comment.category
        and abs(ei.line - comment.line) <= line_tolerance
    ]
    if not candidates:
        return None
    # Prefer the closest by line number.
    candidates.sort(key=lambda pair: abs(pair[1].line - comment.line))
    # Resolve back to the global index in the fixture's expected_issues list.
    # The simple way: scan and identify by identity. For correctness here we
    # just return the position-in-file index; the caller treats expected_issues
    # as flat. We need the flat index.
    # _find_best_match is invoked over expected_by_file which preserved order;
    # rebuild a flat lookup by identity of the matched object.
    best = candidates[0][1]
    # Linear scan to recover flat index. expected_issues is small, this is fine.
    # The caller passed expected_by_file built from fixture.expected_issues.
    # We rebuild the flat list once via the dict's preserved insertion order.
    flat: list = []
    for fname in expected_by_file:
        flat.extend(expected_by_file[fname])
    for i, ei in enumerate(flat):
        if ei is best:
            return i
    return None


# ---- consistency ------------------------------------------------------------


@dataclass
class ConsistencyMetrics:
    """Variance metrics across N repeat runs of the same fixture."""

    runs: int = 0
    pairwise_jaccard: List[float] = field(default_factory=list)
    severity_stability: float = 0.0

    @property
    def mean_jaccard(self) -> float:
        return (
            sum(self.pairwise_jaccard) / len(self.pairwise_jaccard)
            if self.pairwise_jaccard
            else 0.0
        )

    def to_dict(self) -> dict:
        return {
            "runs": self.runs,
            "mean_jaccard": round(self.mean_jaccard, 4),
            "min_jaccard": round(min(self.pairwise_jaccard, default=0.0), 4),
            "max_jaccard": round(max(self.pairwise_jaccard, default=0.0), 4),
            "severity_stability": round(self.severity_stability, 4),
        }


def compute_consistency(
    runs: Sequence[Sequence[FileReviewResult]],
) -> ConsistencyMetrics:
    """
    Score variance across N repeat agent runs over the same fixture input.

    `runs` is a sequence of run-outputs; each run-output is a sequence of
    FileReviewResult objects.
    """
    metrics = ConsistencyMetrics(runs=len(runs))
    if len(runs) < 2:
        return metrics

    # Compute pairwise set Jaccard over (file, line, category) triples.
    finding_sets = [_findings_set(run) for run in runs]
    for i in range(len(finding_sets)):
        for j in range(i + 1, len(finding_sets)):
            metrics.pairwise_jaccard.append(
                _jaccard(finding_sets[i], finding_sets[j])
            )

    # Severity stability: for findings present in run 0, what fraction get
    # the same severity across all subsequent runs?
    metrics.severity_stability = _severity_stability(runs)

    return metrics


def _findings_set(run: Iterable[FileReviewResult]) -> set:
    s = set()
    for result in run:
        for c in result.line_comments:
            s.add((result.filename, c.line, c.category))
    return s


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _severity_stability(runs: Sequence[Sequence[FileReviewResult]]) -> float:
    """For findings that appear in every run, fraction with consistent severity."""
    by_run: list[dict[tuple, str]] = []
    for run in runs:
        sev = {}
        for result in run:
            for c in result.line_comments:
                sev[(result.filename, c.line, c.category)] = c.severity
        by_run.append(sev)

    common_keys = set(by_run[0].keys())
    for d in by_run[1:]:
        common_keys &= set(d.keys())
    if not common_keys:
        return 0.0

    stable = 0
    for key in common_keys:
        if len({d[key] for d in by_run}) == 1:
            stable += 1
    return stable / len(common_keys)


# ---- grounding fidelity (labeling-task emission) ----------------------------


@dataclass
class GroundingTask:
    """A pair surfaced for human grading: did the citation actually apply?"""

    fixture_id: str
    file: str
    line: int
    comment_text: str
    cited_guideline_ids: List[str]
    retrieved_guideline_ids: List[str]
    # Filled in by the human grader after the fact.
    applicable: bool | None = None
    specific: bool | None = None


def emit_grounding_tasks(
    fixture: Fixture,
    results: Sequence[FileReviewResult],
    retrieved_guideline_ids_per_file: dict[str, List[str]] | None = None,
) -> List[GroundingTask]:
    """
    Emit (comment, citation) pairs for human grading.

    Citations are not currently parsed out of the LLM-generated comment body
    automatically; this function emits the comment + the *retrieved* guideline
    IDs and lets the grader judge whether the comment is consistent with any
    retrieved guideline. Once the prompt is updated to require explicit
    citation IDs in the JSON output, this function can be tightened to
    extract cited_guideline_ids structurally.
    """
    retrieved_guideline_ids_per_file = retrieved_guideline_ids_per_file or {}
    tasks: list[GroundingTask] = []
    for result in results:
        retrieved = retrieved_guideline_ids_per_file.get(result.filename, [])
        for c in result.line_comments:
            tasks.append(
                GroundingTask(
                    fixture_id=fixture.fixture_id,
                    file=result.filename,
                    line=c.line,
                    comment_text=f"[{c.severity}] {c.issue} -- {c.suggestion}",
                    cited_guideline_ids=[],  # populated once prompt changes
                    retrieved_guideline_ids=list(retrieved),
                )
            )
    return tasks


@dataclass
class GroundingFidelityMetrics:
    """Aggregate scores over a set of human-labeled GroundingTask objects.

    These metrics are only meaningful once a human grader has filled in the
    ``applicable`` and ``specific`` fields on a labeling-task batch. Tasks
    whose labels are still ``None`` are excluded from each score so that
    partially labeled batches still produce sensible numbers on the labeled
    portion.
    """

    citation_applicability: float = 0.0
    citation_specificity: float = 0.0
    citation_rate: float = 0.0
    n_total_tasks: int = 0
    n_labeled_applicable: int = 0
    n_labeled_specific: int = 0

    def to_dict(self) -> dict:
        return {
            "citation_applicability": round(self.citation_applicability, 4),
            "citation_specificity": round(self.citation_specificity, 4),
            "citation_rate": round(self.citation_rate, 4),
            "n_total_tasks": self.n_total_tasks,
            "n_labeled_applicable": self.n_labeled_applicable,
            "n_labeled_specific": self.n_labeled_specific,
        }


def aggregate_grounding_labels(tasks: Sequence[GroundingTask]) -> GroundingFidelityMetrics:
    """Aggregate human-labeled grounding tasks into fidelity scores.

    - ``citation_rate``: fraction of all tasks where the underlying comment
      cited at least one guideline (structural property; doesn't need labels).
    - ``citation_applicability``: of the tasks where ``applicable`` is set,
      the fraction labeled ``True``.
    - ``citation_specificity``: of the tasks where ``specific`` is set, the
      fraction labeled ``True``.

    Returns zeroed metrics for fields that have no labeled inputs yet rather
    than dividing by zero -- partially labeled batches are the common case.
    """
    metrics = GroundingFidelityMetrics()
    if not tasks:
        return metrics

    metrics.n_total_tasks = len(tasks)

    cited_count = sum(1 for t in tasks if t.cited_guideline_ids)
    metrics.citation_rate = cited_count / len(tasks)

    labeled_applicable = [t for t in tasks if t.applicable is not None]
    metrics.n_labeled_applicable = len(labeled_applicable)
    if labeled_applicable:
        metrics.citation_applicability = (
            sum(1 for t in labeled_applicable if t.applicable) / len(labeled_applicable)
        )

    labeled_specific = [t for t in tasks if t.specific is not None]
    metrics.n_labeled_specific = len(labeled_specific)
    if labeled_specific:
        metrics.citation_specificity = (
            sum(1 for t in labeled_specific if t.specific) / len(labeled_specific)
        )

    return metrics
