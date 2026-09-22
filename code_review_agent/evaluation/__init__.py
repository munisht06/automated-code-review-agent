"""
Evaluation harness for the Automated Code Review Agent.

This package provides the tooling to measure the agent on labeled fixtures:
correctness, citation checks against must_cite, grounding-fidelity labeling
tasks, the RAG-on/RAG-off ablation, and consistency under repeat.

See EVALUATION.md at the repository root for the full methodology.
"""

from .fixtures import (
    ExpectedIssue,
    Fixture,
    NegativeAssertion,
    load_fixture,
    load_fixture_directory,
)
from .metrics import (
    CitationChecks,
    ConsistencyMetrics,
    CorrectnessMetrics,
    GroundingFidelityMetrics,
    GroundingTask,
    NegativeAssertionViolation,
    aggregate_grounding_labels,
    compute_citation_checks,
    compute_consistency,
    compute_correctness,
    emit_grounding_tasks,
    summarize_correctness,
)

__all__ = [
    "Fixture",
    "ExpectedIssue",
    "NegativeAssertion",
    "load_fixture",
    "load_fixture_directory",
    "CitationChecks",
    "CorrectnessMetrics",
    "ConsistencyMetrics",
    "GroundingTask",
    "GroundingFidelityMetrics",
    "NegativeAssertionViolation",
    "compute_correctness",
    "compute_consistency",
    "emit_grounding_tasks",
    "aggregate_grounding_labels",
    "compute_citation_checks",
    "summarize_correctness",
]
