"""
Fixture loading and schema for the evaluation harness.

A fixture is a JSON file describing a single PR (or PR-like change) with
labeled expectations: which issues the agent *should* surface, and which
classes of issue it should *not* falsely flag. Fixtures are the unit of
evaluation; the harness drives the agent against each fixture and scores
the structured output against the labels.

Schema (mirrored in EVALUATION.md):

    {
      "fixture_id": "py-sql-injection-001",
      "language": "python",
      "description": "...",
      "files": [
        {"path": "...", "language": "...", "patch": "...", "content": "..."}
      ],
      "expected_issues": [
        {"file": "...", "line": <int>, "category": "...",
         "severity": "...", "issue_type": "...",
         "must_cite": ["guideline_id", ...]}
      ],
      "negative_assertions": [
        {"file": "...", "category": "...", "rationale": "..."}
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ---- schema dataclasses -----------------------------------------------------


@dataclass
class FixtureFile:
    """A single file within a fixture."""

    path: str
    language: Optional[str]
    patch: str
    content: str


@dataclass
class ExpectedIssue:
    """A positive assertion: the agent should surface this finding."""

    file: str
    line: int
    category: str  # "security" | "style" | "performance" | "bug"
    severity: str  # "CRITICAL" | "WARNING" | "SUGGESTION"
    issue_type: Optional[str] = None  # finer-grained class, e.g. "sql_injection"
    must_cite: List[str] = field(default_factory=list)


@dataclass
class NegativeAssertion:
    """A negative assertion: the agent should NOT flag this category here."""

    file: str
    category: str
    rationale: str = ""


@dataclass
class Fixture:
    """A single labeled PR fixture."""

    fixture_id: str
    language: Optional[str]
    description: str
    files: List[FixtureFile]
    expected_issues: List[ExpectedIssue] = field(default_factory=list)
    negative_assertions: List[NegativeAssertion] = field(default_factory=list)
    source_path: Optional[Path] = None


# ---- loaders ----------------------------------------------------------------


def load_fixture(path: Path | str) -> Fixture:
    """Load a single fixture from a JSON file."""
    p = Path(path)
    with p.open() as f:
        data = json.load(f)

    files = [
        FixtureFile(
            path=ff["path"],
            language=ff.get("language"),
            patch=ff.get("patch", ""),
            content=ff.get("content", ""),
        )
        for ff in data.get("files", [])
    ]

    expected = [
        ExpectedIssue(
            file=ei["file"],
            line=int(ei["line"]),
            category=ei["category"],
            severity=ei["severity"],
            issue_type=ei.get("issue_type"),
            must_cite=list(ei.get("must_cite", [])),
        )
        for ei in data.get("expected_issues", [])
    ]

    negatives = [
        NegativeAssertion(
            file=na["file"],
            category=na["category"],
            rationale=na.get("rationale", ""),
        )
        for na in data.get("negative_assertions", [])
    ]

    return Fixture(
        fixture_id=data["fixture_id"],
        language=data.get("language"),
        description=data.get("description", ""),
        files=files,
        expected_issues=expected,
        negative_assertions=negatives,
        source_path=p,
    )


def load_fixture_directory(directory: Path | str) -> List[Fixture]:
    """Load every *.json file in a directory as a fixture."""
    d = Path(directory)
    fixtures = []
    for p in sorted(d.glob("*.json")):
        try:
            fixtures.append(load_fixture(p))
        except (KeyError, json.JSONDecodeError) as e:
            # Surface schema problems loudly; do not silently skip.
            raise ValueError(f"Failed to load fixture {p}: {e}") from e
    return fixtures
