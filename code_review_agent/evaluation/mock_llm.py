"""
Offline stand-in for the Azure OpenAI client, used by ``runner --mock-llm``.

This exists so the harness can be run end to end without credentials: it
exercises fixture loading, the static scanner, retrieval, prompt
construction, parsing, scoring, and report writing. It is NOT a model and
its output is not a result.

Behavior, all deterministic:

- Embeddings are hashed bag-of-words vectors, so retrieval ranks guidelines
  by lexical overlap with the query rather than by a learned model.
- The "chat completion" echoes the static scanner's findings (the
  "Pre-identified Security Issues" lines of the user prompt) back as review
  comments, citing no guidelines. In other words it behaves like a
  scanner-only configuration.
"""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

_EMBEDDING_DIM = 256
_SCANNER_LINE = re.compile(r"^- Line (\d+): (.+) \((CRITICAL|HIGH|MEDIUM|LOW)\)\s*$", re.MULTILINE)
_SEVERITY_MAP = {
    "CRITICAL": "CRITICAL",
    "HIGH": "WARNING",
    "MEDIUM": "WARNING",
    "LOW": "SUGGESTION",
}


def _hashed_embedding(text: str) -> list[float]:
    vec = [0.0] * _EMBEDDING_DIM
    for token in re.findall(r"[a-z0-9_]+", text.lower()):
        h = int(hashlib.sha256(token.encode()).hexdigest(), 16)
        vec[h % _EMBEDDING_DIM] += 1.0
    if not any(vec):
        vec[0] = 1.0
    return vec


class _Embeddings:
    async def create(self, model: str, input: str):  # noqa: A002 (mirrors the SDK signature)
        return SimpleNamespace(
            model="offline-mock", data=[SimpleNamespace(embedding=_hashed_embedding(input))]
        )


class _Completions:
    async def create(self, model: str, messages: list, **_kwargs):
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        comments = [
            {
                "line": int(line),
                "severity": _SEVERITY_MAP[sev],
                "category": "security",
                "issue": desc,
                "suggestion": "Address the issue reported by the static scanner.",
                "cited_guideline_ids": [],
            }
            for line, desc, sev in _SCANNER_LINE.findall(user)
        ]
        payload = {
            "summary": "Offline mock: static-scanner findings echoed as comments. Not a model output.",
            "comments": comments,
            "style_suggestions": [],
        }
        message = SimpleNamespace(content=json.dumps(payload))
        return SimpleNamespace(model="offline-mock", choices=[SimpleNamespace(message=message)])


class MockAzureClient:
    """Duck-typed replacement for ``AsyncAzureOpenAI`` used in offline runs."""

    def __init__(self) -> None:
        self.embeddings = _Embeddings()
        self.chat = SimpleNamespace(completions=_Completions())
