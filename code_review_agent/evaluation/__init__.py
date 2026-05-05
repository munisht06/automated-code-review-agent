"""
Evaluation harness for the Automated Code Review Agent.

This package provides the methodology and tooling to measure the agent as
a research artifact: correctness on labeled fixtures, grounding fidelity,
RAG-on/RAG-off ablation, and consistency under repeat.

See EVALUATION.md at the repository root for the full methodology.
"""

from .fixtures import (
    Fixture,
    ExpectedIssue,
    NegativeAssertion,
    load_fixture,
    load_fixture_directory,
)
from .metrics import (
    CorrectnessMetrics,
    ConsistencyMetrics,
    GroundingTask,
    compute_correctness,
    compute_consistency,
    emit_grounding_tasks,
)

__all__ = [
    "Fixture",
    "ExpectedIssue",
    "NegativeAssertion",
    "load_fixture",
    "load_fixture_directory",
    "CorrectnessMetrics",
    "ConsistencyMetrics",
    "GroundingTask",
    "compute_correctness",
    "compute_consistency",
    "emit_grounding_tasks",
]
