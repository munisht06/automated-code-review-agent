"""
Automated Code Review Agent

Research prototype: retrieval-grounded LLM code review for GitHub pull
requests, with an evaluation harness under ``code_review_agent.evaluation``.
"""

__version__ = "0.1.0"
__author__ = "Munish Tanwar"

from .github_client import GitHubClient, PRComment, commentable_lines
from .rag_system import GuidelineDocument, GuidelineManager, RAGSystem
from .review_engine import (
    FileReviewResult,
    LineComment,
    ReviewEngine,
    SecurityIssue,
    SecurityScanner,
)

__all__ = [
    "ReviewEngine",
    "SecurityScanner",
    "FileReviewResult",
    "LineComment",
    "SecurityIssue",
    "GitHubClient",
    "PRComment",
    "commentable_lines",
    "RAGSystem",
    "GuidelineDocument",
    "GuidelineManager",
]
