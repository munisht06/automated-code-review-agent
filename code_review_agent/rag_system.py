"""
RAG (Retrieval-Augmented Generation) system for retrieving relevant
coding guidelines and standards to provide context-aware code reviews.
"""

import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import numpy as np
from openai import AsyncAzureOpenAI


@dataclass
class GuidelineDocument:
    """Represents a coding guideline or standard document."""
    id: str
    title: str
    content: str
    language: Optional[str] = None
    category: str = "general"  # security, style, performance, best-practice
    embedding: Optional[list[float]] = None


class RAGSystem:
    """
    Retrieval-Augmented Generation system for coding guidelines.
    Uses vector embeddings to find relevant guidelines for code review context.
    """
    
    def __init__(self, guidelines_path: str = "guidelines"):
        self.client = AsyncAzureOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=os.getenv("AZURE_OPENAI_KEY"),
            api_version="2024-02-15-preview"
        )
        self.embedding_model = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")
        self.guidelines_path = Path(guidelines_path)
        self.guidelines: list[GuidelineDocument] = []
        self.embeddings_cache: dict[str, list[float]] = {}
        
    async def initialize(self):
        """Load and embed all guidelines documents."""
        await self._load_guidelines()
        await self._compute_embeddings()
    
    async def _load_guidelines(self):
        """Load guidelines from markdown/JSON files."""
        self.guidelines = []
        
        # Load from files if they exist
        if self.guidelines_path.exists():
            for file in self.guidelines_path.glob("**/*.md"):
                content = file.read_text()
                self.guidelines.append(GuidelineDocument(
                    id=file.stem,
                    title=file.stem.replace("_", " ").title(),
                    content=content,
                    language=self._detect_language(file.stem),
                    category=self._detect_category(file.parent.name)
                ))
        
        # Add default guidelines if none loaded
        if not self.guidelines:
            self.guidelines = self._get_default_guidelines()
    
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
                category="style"
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
                category="security"
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
                category="best-practice"
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
                category="performance"
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
                category="style"
            )
        ]
    
    async def _compute_embeddings(self):
        """Compute embeddings for all guidelines."""
        for guideline in self.guidelines:
            if guideline.id not in self.embeddings_cache:
                embedding = await self._get_embedding(
                    f"{guideline.title}\n{guideline.content}"
                )
                self.embeddings_cache[guideline.id] = embedding
                guideline.embedding = embedding
    
    async def _get_embedding(self, text: str) -> list[float]:
        """Get embedding vector for text using Azure OpenAI."""
        response = await self.client.embeddings.create(
            model=self.embedding_model,
            input=text[:8000]  # Truncate to token limit
        )
        return response.data[0].embedding
    
    async def retrieve_guidelines(
        self,
        filename: str,
        code_snippet: str,
        top_k: int = 3
    ) -> list[GuidelineDocument]:
        """
        Retrieve most relevant guidelines for the given code context.
        Uses semantic similarity to find applicable guidelines.
        """
        # Ensure embeddings are computed
        if not self.embeddings_cache:
            await self.initialize()
        
        # Build query from code context
        language = self._detect_language(filename)
        query = f"Code review for {language} file: {filename}\n{code_snippet[:1000]}"
        
        # Get query embedding
        query_embedding = await self._get_embedding(query)
        
        # Calculate similarities
        similarities = []
        for guideline in self.guidelines:
            if guideline.embedding:
                sim = self._cosine_similarity(query_embedding, guideline.embedding)
                
                # Boost score if language matches
                if guideline.language and guideline.language == language:
                    sim *= 1.3
                
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
    def _detect_language(filename: str) -> Optional[str]:
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
            ".rb": "ruby"
        }
        for ext, lang in ext_map.items():
            if filename.endswith(ext):
                return lang
        return None
    
    @staticmethod
    def _detect_category(folder_name: str) -> str:
        """Detect guideline category from folder name."""
        categories = {"security", "style", "performance", "best-practice"}
        return folder_name if folder_name in categories else "general"


class GuidelineManager:
    """Manage custom guidelines for specific repositories."""
    
    def __init__(self, storage_path: str = "repo_guidelines"):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(exist_ok=True)
    
    def save_repo_guidelines(self, repo: str, guidelines: list[dict]):
        """Save custom guidelines for a repository."""
        repo_file = self.storage_path / f"{repo.replace('/', '_')}.json"
        with open(repo_file, 'w') as f:
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
                category=g.get("category", "custom")
            )
            for i, g in enumerate(data)
        ]