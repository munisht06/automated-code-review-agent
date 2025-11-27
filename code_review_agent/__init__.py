"""
Automated Code Review Agent

An AI-powered code review system using Azure OpenAI and RAG techniques.
"""

__version__ = "1.0.0"
__author__ = "Munish Tanwar"

from .review_engine import (
    ReviewEngine,
    SecurityScanner,
    FileReviewResult,
    LineComment,
    SecurityIssue,
)
from .github_client import GitHubClient, PRComment
from .rag_system import RAGSystem, GuidelineDocument, GuidelineManager

__all__ = [
    "ReviewEngine",
    "SecurityScanner",
    "FileReviewResult",
    "LineComment",
    "SecurityIssue",
    "GitHubClient",
    "PRComment",
    "RAGSystem",
    "GuidelineDocument",
    "GuidelineManager",
]
