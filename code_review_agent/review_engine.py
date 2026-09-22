import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncAzureOpenAI

from .rag_system import RAGSystem, azure_api_version

logger = logging.getLogger(__name__)

# Prompt budgets, in characters. Text beyond a budget is cut, and the cut is
# made visible twice: a marker in the prompt tells the model, and the
# FileReviewResult records it for the evaluation report. An earlier 500-char
# guideline cap removed the Security section of every bundled guideline with
# no signal anywhere, which is why a cut is never silent now.
MAX_GUIDELINE_CHARS = 4000
MAX_PATCH_CHARS = 2000
MAX_FILE_CONTENT_CHARS = 4000

# Number of static-scanner findings passed into the prompt, highest severity first.
MAX_SCANNER_FINDINGS_IN_PROMPT = 5

LLM_TEMPERATURE = 0.1


@dataclass
class SecurityIssue:
    """Represents a security vulnerability found in code."""

    type: str
    severity: str
    line: int
    description: str
    recommendation: str


@dataclass
class LineComment:
    """Represents a single line comment in code review."""

    line: int
    severity: str  # "CRITICAL", "WARNING", "SUGGESTION"
    category: str  # "security", "style", "performance", "bug"
    issue: str
    suggestion: str
    # IDs of the retrieved guidelines the model says this comment relies on.
    cited_guideline_ids: list[str] = field(default_factory=list)


@dataclass
class FileReviewResult:
    """Result of reviewing a single file."""

    filename: str
    summary: str
    line_comments: list[LineComment] = field(default_factory=list)
    security_issues: list[SecurityIssue] = field(default_factory=list)
    style_suggestions: list[str] = field(default_factory=list)
    # Provenance, recorded so a result can be traced to its inputs.
    retrieved_guideline_ids: list[str] = field(default_factory=list)
    truncated_inputs: list[str] = field(default_factory=list)
    parse_error: str | None = None
    raw_response: str | None = None
    # Model name reported in the API response (the model behind the deployment).
    response_model: str | None = None
    # Set by the evaluation harness when the review call itself failed
    # (network, API or content-filter error), as distinct from a response
    # that arrived but did not parse.
    call_error: str | None = None


def clip_parts(text: str, limit: int) -> tuple[str, str]:
    """Cut ``text`` to at most ``limit`` characters, at the last line break
    when there is one, and return (kept text, truncation marker or "")."""
    if len(text) <= limit:
        return text, ""
    cut = text.rfind("\n", 0, limit + 1)
    kept = text[:cut] if cut > 0 else text[:limit]
    return kept, f"[... truncated: showing {len(kept)} of {len(text)} characters]"


def clip(text: str, limit: int) -> tuple[str, bool]:
    """Cut ``text`` to its budget, appending a visible marker if cut."""
    kept, marker = clip_parts(text, limit)
    return (f"{kept}\n{marker}", True) if marker else (kept, False)


def number_lines(text: str) -> str:
    """Prefix each line with its 1-based line number, so the model does not
    have to count lines to report them."""
    return "\n".join(f"{i:>4} | {line}" for i, line in enumerate(text.split("\n"), start=1))


class SecurityScanner:
    """
    Static security scanner that detects common vulnerabilities.
    Implements pattern-based security analysis for various languages.

    Patterns are matched line by line and case-insensitively, except where a
    pattern scopes case sensitivity itself with ``(?-i:...)``.
    """

    # Security patterns to detect
    PATTERNS = {
        "hardcoded_secret": [
            (r'password\s*=\s*["\'][^"\']{3,}["\']', "Hardcoded password detected"),
            (r'api[_-]?key\s*=\s*["\'][^"\']{10,}["\']', "Hardcoded API key detected"),
            (r'secret\s*=\s*["\'][^"\']{10,}["\']', "Hardcoded secret detected"),
            (r'token\s*=\s*["\'][^"\']{10,}["\']', "Hardcoded token detected"),
            # A string literal assigned to an AWS secret, not a read from the environment.
            # Assignment, dict entry or quoted YAML value: aws_secret... = "...", "aws_secret...": "...".
            (r'aws[_-]?secret\w*["\']?\s*[=:]\s*["\'][^"\']{10,}["\']', "AWS credentials detected"),
        ],
        "sql_injection": [
            (r'execute\s*\(\s*f["\'].*?\{.*?\}', "Potential SQL injection via f-string"),
            # A string literal followed by + inside execute(...), not a + inside the
            # literal. The backreference closes the literal with its own quote, so
            # "... name = '" + name still matches.
            (
                r'execute\s*\(\s*(["\'])(?:(?!\1).)*\1\s*\+',
                "Potential SQL injection via string concatenation",
            ),
            # A SQL statement built by concatenation, then executed elsewhere.
            (
                r'=\s*(["\'])\s*(SELECT|INSERT|UPDATE|DELETE)\b(?:(?!\1).)*\1\s*\+',
                "SQL query built by string concatenation",
            ),
            (r'query\s*=\s*f["\']SELECT.*?\{', "SQL query with f-string interpolation"),
            (r"\.format\s*\(.*?\).*?execute", "SQL query with .format() method"),
        ],
        "command_injection": [
            (r"os\.system\s*\(.*?\+.*?\)", "Command injection via os.system"),
            (r"subprocess\.(call|run|Popen)\s*\(.*?shell\s*=\s*True", "Shell injection risk"),
            # Bare eval()/exec() only: not model.eval(), ast.literal_eval(), or regex.exec().
            (r"(?<![\w.])eval\s*\(", "Use of eval() is dangerous"),
            (r"(?<![\w.])exec\s*\(", "Use of exec() is dangerous"),
        ],
        "xss_vulnerability": [
            (r"innerHTML\s*=\s*.*?\+", "Potential XSS via innerHTML"),
            (r"dangerouslySetInnerHTML", "React XSS risk with dangerouslySetInnerHTML"),
            (r"document\.write\s*\(", "XSS risk with document.write"),
        ],
        "path_traversal": [
            (r'open\s*\(.*?\+.*?["\']\.\.', "Path traversal vulnerability"),
            # A capitalized File constructor (Java, C#, Kotlin) taking a user-derived path.
            # Case-sensitive so read_file(...) and get_profile(...) do not match.
            (r"(?<![\w.])(?-i:File)\s*\([^)]*\buser", "User-controlled file path"),
        ],
    }

    @classmethod
    def scan(cls, code: str, language: str | None = None) -> list[dict[str, Any]]:
        """
        Scan code for security vulnerabilities.

        Args:
            code: Source code to scan
            language: Programming language. Accepted for future language-specific
                rules; currently unused, so every pattern runs on every file.

        Returns:
            List of security issues found
        """
        issues = []
        lines = code.split("\n")

        for vuln_type, patterns in cls.PATTERNS.items():
            for pattern, description in patterns:
                for i, line in enumerate(lines, start=1):
                    if re.search(pattern, line, re.IGNORECASE):
                        issues.append(
                            {
                                "type": vuln_type,
                                "severity": cls._get_severity(vuln_type),
                                "line": i,
                                "description": description,
                                "code_snippet": line.strip(),
                                "recommendation": cls._get_recommendation(vuln_type),
                            }
                        )

        return issues

    @staticmethod
    def _get_severity(vuln_type: str) -> str:
        """Get severity level for vulnerability type."""
        critical = {"sql_injection", "command_injection", "path_traversal"}
        high = {"hardcoded_secret", "xss_vulnerability"}

        if vuln_type in critical:
            return "CRITICAL"
        elif vuln_type in high:
            return "HIGH"
        else:
            return "MEDIUM"

    @staticmethod
    def _get_recommendation(vuln_type: str) -> str:
        """Get remediation recommendation for vulnerability type."""
        recommendations = {
            "hardcoded_secret": "Use environment variables or a secrets management service",
            "sql_injection": "Use parameterized queries or prepared statements",
            "command_injection": "Avoid shell=True, use subprocess with list arguments",
            "xss_vulnerability": "Sanitize user input and use safe rendering methods",
            "path_traversal": "Validate and sanitize file paths, use allowlist approach",
        }
        return recommendations.get(vuln_type, "Review and fix the security issue")


class ReviewEngine:
    """
    Code review engine: one LLM call per file, grounded in retrieved
    guidelines and given the static scanner's findings as evidence.
    """

    def __init__(self, client=None, rag_system: RAGSystem | None = None, rag_enabled: bool = True):
        # The Azure client is created lazily unless one is injected (the
        # evaluation harness injects an offline stand-in in mock mode).
        self._client = client
        self.rag_system = rag_system if rag_system is not None else RAGSystem(client=client)
        # When False, no guidelines are retrieved or placed in the prompt:
        # the RAG-off arm of the ablation.
        self.rag_enabled = rag_enabled
        self.deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
        self.security_scanner = SecurityScanner()

    @property
    def client(self):
        if self._client is None:
            self._client = AsyncAzureOpenAI(
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                api_key=os.getenv("AZURE_OPENAI_KEY"),
                api_version=azure_api_version(),
            )
        return self._client

    async def review_file(self, filename: str, patch: str, file_content: str) -> FileReviewResult:
        """
        Review one file: static scan, guideline retrieval, one LLM call.

        Args:
            filename: Name of the file being reviewed
            patch: Git diff patch showing changes
            file_content: Full content of the file

        Returns:
            FileReviewResult with summary, comments, security issues, and the
            provenance needed to audit the result.
        """
        # 1. Run static security scan
        security_issues = self.security_scanner.scan(
            file_content, language=RAGSystem._detect_language(filename)
        )

        # 2. Retrieve relevant guidelines using RAG
        if self.rag_enabled:
            guidelines = await self.rag_system.retrieve_guidelines(filename, file_content)
        else:
            guidelines = []

        # 3. Build prompts
        system_prompt = self._build_system_prompt(guidelines)
        user_prompt = self._build_user_prompt(filename, patch, file_content, security_issues)

        # 4. One LLM call. JSON mode returns syntactically valid JSON unless the
        # response is cut off at the token limit; the field structure is
        # requested in the prompt and checked when parsing.
        response = await self.client.chat.completions.create(
            model=self.deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=LLM_TEMPERATURE,
            response_format={"type": "json_object"},
        )

        # 5. Parse, then attach provenance
        result = self._parse_review_response(filename, response, security_issues)
        result.response_model = getattr(response, "model", None)
        result.retrieved_guideline_ids = [g.id for g in guidelines]
        result.truncated_inputs = self._prompt_truncations(guidelines, patch, file_content)
        return result

    @staticmethod
    def _prompt_truncations(guidelines: list, patch: str, file_content: str) -> list[str]:
        """Describe every input that exceeded its prompt budget."""
        cuts = []
        for g in guidelines:
            if len(g.content) > MAX_GUIDELINE_CHARS:
                cuts.append(f"guideline {g.id}: {len(g.content)} > {MAX_GUIDELINE_CHARS} chars")
        if len(patch) > MAX_PATCH_CHARS:
            cuts.append(f"patch: {len(patch)} > {MAX_PATCH_CHARS} chars")
        if len(file_content) > MAX_FILE_CONTENT_CHARS:
            cuts.append(f"file content: {len(file_content)} > {MAX_FILE_CONTENT_CHARS} chars")
        return cuts

    def _build_system_prompt(self, guidelines: list) -> str:
        """Build system prompt with guidelines and output format specification."""
        if guidelines:
            guidelines_text = "\n\n".join(
                f"## {g.title} (id: {g.id})\n{clip(g.content, MAX_GUIDELINE_CHARS)[0]}"
                for g in guidelines
            )
        else:
            guidelines_text = "(No guidelines are provided for this review.)"

        return f"""You are an expert code reviewer specializing in security, performance, and code quality.

Your task is to perform a thorough code review. Guidelines for this review:

{guidelines_text}

**Review Focus Areas:**
1. Security vulnerabilities (SQL injection, XSS, hardcoded secrets, etc.)
2. Code style and best practices violations
3. Performance issues and anti-patterns
4. Potential bugs and logic errors
5. Maintainability concerns

**Output Format:**
You MUST respond with valid JSON in this exact structure:
{{
  "summary": "Brief overall assessment (2-3 sentences)",
  "comments": [
    {{
      "line": <line_number>,
      "severity": "CRITICAL|WARNING|SUGGESTION",
      "category": "security|style|performance|bug",
      "issue": "What is wrong",
      "suggestion": "How to fix it",
      "cited_guideline_ids": ["<id of each guideline above that this comment applies, or an empty list>"]
    }}
  ],
  "style_suggestions": ["suggestion1", "suggestion2"]
}}

Line numbers refer to the numbered file content in the user message. When a comment applies one of the guidelines above, put that guideline's id in cited_guideline_ids.

Be constructive, specific, and actionable. Focus on high-impact issues."""

    # Severity ranking for prompt prioritization (lower number = higher priority).
    _SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

    def _build_user_prompt(
        self, filename: str, patch: str, file_content: str, security_issues: list[dict]
    ) -> str:
        """Build user prompt with code context and pre-scanned security issues."""
        security_context = ""
        if security_issues:
            # Prioritize by severity so the prompt's budget surfaces the most
            # important findings, not whatever the regex catalog matched first.
            sorted_issues = sorted(
                security_issues,
                key=lambda i: self._SEVERITY_RANK.get(i.get("severity", "LOW"), 99),
            )
            security_context = "\n**Pre-identified Security Issues:**\n"
            for issue in sorted_issues[:MAX_SCANNER_FINDINGS_IN_PROMPT]:
                security_context += (
                    f"- Line {issue['line']}: {issue['description']} ({issue['severity']})\n"
                )

        patch_text, _ = clip(patch, MAX_PATCH_CHARS)
        # Number the kept lines first, then add the marker unnumbered, so the
        # marker cannot be mistaken for a source line.
        kept_content, content_marker = clip_parts(file_content, MAX_FILE_CONTENT_CHARS)
        numbered_content = number_lines(kept_content)
        if content_marker:
            numbered_content += "\n" + content_marker

        return f"""**File:** `{filename}`

{security_context}

**Changes (Git Diff):**
```diff
{patch_text}
```

**Full File Content (line-numbered):**
```
{numbered_content}
```

Please provide a comprehensive code review in the specified JSON format."""

    @staticmethod
    def _coerce_line(value) -> int | None:
        """Return a line number >= 1 from an int, a whole float or a string of
        ASCII digits; ``None`` for anything else."""
        if isinstance(value, bool):
            return None
        if isinstance(value, float):
            number = int(value) if value.is_integer() else None
        elif isinstance(value, int):
            number = value
        elif isinstance(value, str) and re.fullmatch(r"\s*[0-9]+\s*", value):
            number = int(value)
        else:
            number = None
        return number if number is not None and number >= 1 else None

    def _parse_review_response(
        self, filename: str, response, security_issues: list[dict]
    ) -> FileReviewResult:
        """Parse LLM response and construct FileReviewResult.

        Scanner findings are kept whether or not the LLM output parses, and a
        parse failure is recorded in ``parse_error`` rather than disguised as
        "no findings".
        """
        sec_issues = [
            SecurityIssue(
                type=issue["type"],
                severity=issue["severity"],
                line=issue["line"],
                description=issue["description"],
                recommendation=issue["recommendation"],
            )
            for issue in security_issues
        ]

        raw = None
        try:
            raw = response.choices[0].message.content
            if raw is None:
                raise ValueError("empty response content")
            review_data = json.loads(raw)
            if not isinstance(review_data, dict):
                raise ValueError("response JSON is not an object")

            comments = review_data.get("comments") or []
            if not isinstance(comments, list):
                raise ValueError("'comments' is not a list")

            line_comments = []
            dropped = 0
            for comment_data in comments:
                if not isinstance(comment_data, dict):
                    dropped += 1
                    continue
                line = self._coerce_line(comment_data.get("line"))
                if line is None:
                    dropped += 1
                    continue
                cited = comment_data.get("cited_guideline_ids") or []
                if not isinstance(cited, list):
                    cited = []
                line_comments.append(
                    LineComment(
                        line=line,
                        severity=str(comment_data.get("severity", "SUGGESTION")).strip().upper(),
                        category=str(comment_data.get("category", "general")).strip().lower(),
                        issue=str(comment_data.get("issue", "")),
                        suggestion=str(comment_data.get("suggestion", "")),
                        cited_guideline_ids=[str(c) for c in cited if isinstance(c, (str, int))],
                    )
                )

            style = review_data.get("style_suggestions") or []
            if not isinstance(style, list):
                style = []

            return FileReviewResult(
                filename=filename,
                summary=str(review_data.get("summary", "Review completed.")),
                line_comments=line_comments,
                security_issues=sec_issues,
                style_suggestions=[str(s) for s in style],
                parse_error=(f"dropped {dropped} malformed comment(s)" if dropped else None),
                raw_response=raw,
            )

        except (
            json.JSONDecodeError,
            KeyError,
            AttributeError,
            IndexError,
            TypeError,
            ValueError,
        ) as e:
            logger.warning("Error parsing review response for %s: %s", filename, e)
            return FileReviewResult(
                filename=filename,
                summary="Review completed with parsing errors.",
                line_comments=[],
                security_issues=sec_issues,
                style_suggestions=[],
                parse_error=f"{type(e).__name__}: {e}",
                raw_response=raw if isinstance(raw, str) else None,
            )
