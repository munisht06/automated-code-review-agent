"""
RAG (Retrieval-Augmented Generation) system for retrieving relevant
coding guidelines and standards to provide context-aware code reviews.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from openai import AsyncAzureOpenAI

logger = logging.getLogger(__name__)

# The bundled corpus lives at the repository root, next to this package.
# Resolving it relative to the package (rather than the working directory)
# keeps the corpus the same no matter where the harness is launched from.
DEFAULT_GUIDELINES_PATH = Path(__file__).resolve().parent.parent / "guidelines"

# Characters of text sent to the embedding model per call.
MAX_EMBEDDING_INPUT_CHARS = 8000
# Characters of the file under review used to build the retrieval query.
RETRIEVAL_QUERY_CHARS = 1000
# Number of guidelines returned per file. With the bundled two-document
# corpus this exceeds the corpus size, so every guideline is returned and
# similarity only determines their order.
DEFAULT_TOP_K = 3
# Multiplier applied to the similarity of guidelines whose language matches
# the file under review.
LANGUAGE_MATCH_BOOST = 1.3

DEFAULT_AZURE_API_VERSION = "2024-02-15-preview"


def azure_api_version() -> str:
    """The Azure OpenAI API version, read when a client is created (not at
    import time) so that a value loaded from ``.env`` takes effect."""
    return os.getenv("AZURE_OPENAI_API_VERSION", DEFAULT_AZURE_API_VERSION)


@dataclass
class GuidelineDocument:
    """Represents a coding guideline or standard document."""

    id: str
    title: str
    content: str
    language: str | None = None
    category: str = "general"  # security, style, performance, best-practice
    embedding: list[float] | None = None


class RAGSystem:
    """
    Retrieval-Augmented Generation system for coding guidelines.
    Uses vector embeddings to find relevant guidelines for code review context.
    """

    def __init__(self, guidelines_path: str | Path | None = None, client=None):
        # The Azure client is created lazily on first use, so the class can be
        # constructed (and its pure helpers tested) without credentials. A
        # client can also be injected, which is how the offline mock mode of
        # the evaluation harness works.
        self._client = client
        self.embedding_model = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")
        self.guidelines_path = Path(guidelines_path) if guidelines_path else DEFAULT_GUIDELINES_PATH
        self.guidelines: list[GuidelineDocument] = []
        self.embeddings_cache: dict[str, list[float]] = {}
        # Where the loaded corpus came from, recorded in evaluation reports.
        self.corpus_source: str | None = None
        # Model name reported by the embeddings API (the model behind the deployment).
        self.embedding_response_model: str | None = None

    @property
    def client(self):
        if self._client is None:
            self._client = AsyncAzureOpenAI(
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                api_key=os.getenv("AZURE_OPENAI_KEY"),
                api_version=azure_api_version(),
            )
        return self._client

    async def initialize(self):
        """Load and embed all guidelines documents."""
        await self._load_guidelines()
        await self._compute_embeddings()

    async def _load_guidelines(self):
        """Load guidelines from the markdown files under ``guidelines_path``."""
        self.guidelines = []

        # Load from files if they exist
        if self.guidelines_path.exists():
            for file in self.guidelines_path.glob("**/*.md"):
                content = file.read_text()
                self.guidelines.append(
                    GuidelineDocument(
                        id=file.stem,
                        title=file.stem.replace("_", " ").title(),
                        content=content,
                        language=self._detect_guideline_language(file.stem),
                        # Pass both folder name and filename: folder takes precedence
                        # when guidelines/ has category subfolders, but when files
                        # live directly under guidelines/ (the common case), the
                        # filename is what carries the category signal.
                        category=self._detect_category(file.parent.name, file.stem),
                    )
                )

        if self.guidelines:
            self.corpus_source = str(self.guidelines_path)
        else:
            # Fall back to the built-in defaults, and say so: a silent swap of
            # the corpus changes every retrieval-dependent result.
            self.guidelines = self._get_default_guidelines()
            self.corpus_source = "built-in defaults"
            logger.warning(
                "No guideline files found at %s; using built-in default guidelines.",
                self.guidelines_path,
            )

    def _get_default_guidelines(self) -> list[GuidelineDocument]:
        """Provide sensible default guidelines."""
        return [
            GuidelineDocument(
                id="python_style",
                title="Python Style Guide",
                content="""## Python Style Guidelines
- Follow PEP 8 for code formatting
- Use meaningful variable names (avoid single letters except for loops)
- Keep functions under 50 lines, classes under 300 lines
- Use type hints for function parameters and return values
- Write docstrings for all public functions and classes
- Prefer list comprehensions over map/filter for simple cases
- Use context managers (with statements) for resource handling
- Avoid mutable default arguments in function definitions""",
                language="python",
                category="style",
            ),
            GuidelineDocument(
                id="security_best_practices",
                title="Security Best Practices",
                content="""## Security Guidelines
- Never hardcode secrets, API keys, or passwords
- Use parameterized queries to prevent SQL injection
- Sanitize and validate all user inputs
- Use HTTPS for all external communications
- Implement proper authentication and authorization
- Log security events but never log sensitive data
- Keep dependencies updated to patch vulnerabilities
- Use secure random generators for tokens/secrets""",
                category="security",
            ),
            GuidelineDocument(
                id="error_handling",
                title="Error Handling Standards",
                content="""## Error Handling Guidelines
- Use specific exception types, not bare except clauses
- Always log exceptions with full context
- Provide meaningful error messages to users
- Implement retry logic for transient failures
- Use circuit breakers for external service calls
- Clean up resources in finally blocks
- Don't swallow exceptions silently""",
                category="best-practice",
            ),
            GuidelineDocument(
                id="performance",
                title="Performance Guidelines",
                content="""## Performance Best Practices
- Avoid N+1 query problems in database access
- Use pagination for large data sets
- Implement caching for expensive operations
- Use async/await for I/O-bound operations
- Profile before optimizing
- Avoid premature optimization
- Use appropriate data structures (sets for lookups, etc.)""",
                category="performance",
            ),
            GuidelineDocument(
                id="typescript_react",
                title="TypeScript/React Guidelines",
                content="""## TypeScript/React Best Practices
- Use TypeScript strict mode
- Define explicit types for props and state
- Use functional components with hooks
- Memoize expensive computations with useMemo
- Avoid inline function definitions in JSX
- Use proper dependency arrays in useEffect
- Implement error boundaries for component trees
- Keep components small and focused""",
                language="typescript",
                category="style",
            ),
        ]

    async def _compute_embeddings(self):
        """Compute embeddings for all guidelines.

        All-or-nothing: new embeddings are stored only after every call has
        succeeded. A failure part-way through would otherwise leave some
        guidelines without an embedding, and retrieval would silently skip
        them for the rest of the process.
        """
        new: dict[str, list[float]] = {}
        for guideline in self.guidelines:
            if guideline.id not in self.embeddings_cache:
                new[guideline.id] = await self._get_embedding(
                    f"{guideline.title}\n{guideline.content}"
                )
        self.embeddings_cache.update(new)
        for guideline in self.guidelines:
            guideline.embedding = self.embeddings_cache[guideline.id]

    async def _get_embedding(self, text: str) -> list[float]:
        """Get embedding vector for text using Azure OpenAI."""
        response = await self.client.embeddings.create(
            model=self.embedding_model, input=text[:MAX_EMBEDDING_INPUT_CHARS]
        )
        self.embedding_response_model = getattr(response, "model", None)
        embedding: list[float] = response.data[0].embedding
        return embedding

    async def retrieve_guidelines(
        self, filename: str, code_snippet: str, top_k: int = DEFAULT_TOP_K
    ) -> list[GuidelineDocument]:
        """
        Retrieve most relevant guidelines for the given code context.
        Uses semantic similarity to find applicable guidelines.
        """
        # Ensure every guideline is embedded (retried if an earlier attempt failed)
        if not self.guidelines or any(g.embedding is None for g in self.guidelines):
            await self.initialize()

        # Build query from code context
        language = self._detect_language(filename)
        query = (
            f"Code review for {language} file: {filename}\n{code_snippet[:RETRIEVAL_QUERY_CHARS]}"
        )

        # Get query embedding
        query_embedding = await self._get_embedding(query)

        # Calculate similarities
        similarities = []
        for guideline in self.guidelines:
            if guideline.embedding:
                sim = self._cosine_similarity(query_embedding, guideline.embedding)

                # Boost score if language matches
                if guideline.language and guideline.language == language:
                    sim *= LANGUAGE_MATCH_BOOST

                similarities.append((guideline, sim))

        # Sort by similarity and return top_k
        similarities.sort(key=lambda x: x[1], reverse=True)
        return [g for g, _ in similarities[:top_k]]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        a_arr, b_arr = np.array(a), np.array(b)
        return float(np.dot(a_arr, b_arr) / (np.linalg.norm(a_arr) * np.linalg.norm(b_arr)))

    @staticmethod
    def _detect_language(filename: str) -> str | None:
        """Detect programming language from filename."""
        ext_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".jsx": "javascript",
            ".java": "java",
            ".cs": "csharp",
            ".go": "go",
            ".rs": "rust",
            ".rb": "ruby",
        }
        for ext, lang in ext_map.items():
            if filename.endswith(ext):
                return lang
        return None

    @staticmethod
    def _detect_guideline_language(stem: str) -> str | None:
        """Detect the language a guideline file covers from its file name.

        Guideline files are named for their language (``python_best_practices``,
        ``typescript_react_standards``) rather than given a source-file
        extension, so ``_detect_language`` cannot be used on them: it returns
        ``None`` for every guideline, and the language-match boost never applies.
        """
        prefixes = {
            "python": "python",
            "typescript": "typescript",
            "javascript": "javascript",
            "java": "java",
            "csharp": "csharp",
            "go": "go",
            "golang": "go",
            "rust": "rust",
            "ruby": "ruby",
        }
        first = stem.lower().split("_")[0].split("-")[0]
        return prefixes.get(first)

    @staticmethod
    def _detect_category(folder_name: str, filename: str = "") -> str:
        """Detect guideline category from folder name, falling back to filename.

        When guidelines are organized into category-named subfolders
        (``guidelines/security/api.md``), the folder name carries the signal.
        When they live directly under ``guidelines/`` (the current default
        layout), fall back to substring inference on the filename so we don't
        label every file ``general``.
        """
        categories = {"security", "style", "performance", "best-practice"}
        if folder_name in categories:
            return folder_name
        name = filename.lower()
        for cat in categories:
            if cat in name:
                return cat
        if "best_practice" in name or "standards" in name:
            return "best-practice"
        return "general"


class GuidelineManager:
    """Manage custom guidelines for specific repositories."""

    def __init__(self, storage_path: str = "repo_guidelines"):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(exist_ok=True)

    def save_repo_guidelines(self, repo: str, guidelines: list[dict]):
        """Save custom guidelines for a repository."""
        repo_file = self.storage_path / f"{repo.replace('/', '_')}.json"
        with open(repo_file, "w") as f:
            json.dump(guidelines, f, indent=2)

    def load_repo_guidelines(self, repo: str) -> list[GuidelineDocument]:
        """Load custom guidelines for a repository."""
        repo_file = self.storage_path / f"{repo.replace('/', '_')}.json"
        if not repo_file.exists():
            return []

        with open(repo_file) as f:
            data = json.load(f)

        return [
            GuidelineDocument(
                id=g.get("id", f"custom_{i}"),
                title=g.get("title", "Custom Guideline"),
                content=g.get("content", ""),
                language=g.get("language"),
                category=g.get("category", "custom"),
            )
            for i, g in enumerate(data)
        ]
