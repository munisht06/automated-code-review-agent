from dataclasses import dataclass, field
import os
import re
import json
from typing import List, Dict, Any, Optional
from openai import AsyncAzureOpenAI
from .rag_system import RAGSystem


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


@dataclass
class FileReviewResult:
    """Result of reviewing a single file."""
    filename: str
    summary: str
    line_comments: List[LineComment] = field(default_factory=list)
    security_issues: List[SecurityIssue] = field(default_factory=list)
    style_suggestions: List[str] = field(default_factory=list)


class SecurityScanner:
    """
    Static security scanner that detects common vulnerabilities.
    Implements pattern-based security analysis for various languages.
    """

    # Security patterns to detect
    PATTERNS = {
        "hardcoded_secret": [
            (r'password\s*=\s*["\'][^"\']{3,}["\']', "Hardcoded password detected"),
            (r'api[_-]?key\s*=\s*["\'][^"\']{10,}["\']', "Hardcoded API key detected"),
            (r'secret\s*=\s*["\'][^"\']{10,}["\']', "Hardcoded secret detected"),
            (r'token\s*=\s*["\'][^"\']{10,}["\']', "Hardcoded token detected"),
            (r'aws[_-]?secret', "AWS credentials detected"),
        ],
        "sql_injection": [
            (r'execute\s*\(\s*f["\'].*?\{.*?\}', "Potential SQL injection via f-string"),
            (r'execute\s*\(\s*["\'].*?\+.*?["\']', "Potential SQL injection via string concatenation"),
            (r'query\s*=\s*f["\']SELECT.*?\{', "SQL query with f-string interpolation"),
            (r'\.format\s*\(.*?\).*?execute', "SQL query with .format() method"),
        ],
        "command_injection": [
            (r'os\.system\s*\(.*?\+.*?\)', "Command injection via os.system"),
            (r'subprocess\.(call|run|Popen)\s*\(.*?shell\s*=\s*True', "Shell injection risk"),
            (r'eval\s*\(', "Use of eval() is dangerous"),
            (r'exec\s*\(', "Use of exec() is dangerous"),
        ],
        "xss_vulnerability": [
            (r'innerHTML\s*=\s*.*?\+', "Potential XSS via innerHTML"),
            (r'dangerouslySetInnerHTML', "React XSS risk with dangerouslySetInnerHTML"),
            (r'document\.write\s*\(', "XSS risk with document.write"),
        ],
        "path_traversal": [
            (r'open\s*\(.*?\+.*?["\']\.\.', "Path traversal vulnerability"),
            (r'File\s*\(.*?user.*?\)', "User-controlled file path"),
        ],
    }

    @classmethod
    def scan(cls, code: str, language: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Scan code for security vulnerabilities.

        Args:
            code: Source code to scan
            language: Programming language (optional, for language-specific rules)

        Returns:
            List of security issues found
        """
        issues = []
        lines = code.split('\n')

        for vuln_type, patterns in cls.PATTERNS.items():
            for pattern, description in patterns:
                for i, line in enumerate(lines, start=1):
                    if re.search(pattern, line, re.IGNORECASE):
                        issues.append({
                            "type": vuln_type,
                            "severity": cls._get_severity(vuln_type),
                            "line": i,
                            "description": description,
                            "code_snippet": line.strip(),
                            "recommendation": cls._get_recommendation(vuln_type)
                        })

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
    AI-powered code review engine with RAG and security scanning.
    Orchestrates LLM-based reviews with context-aware guidelines.
    """

    def __init__(self):
        self.client = AsyncAzureOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=os.getenv("AZURE_OPENAI_KEY"),
            api_version="2024-02-15-preview"
        )
        self.rag_system = RAGSystem()
        self.deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
        self.security_scanner = SecurityScanner()

    async def review_file(self, filename: str, patch: str, file_content: str) -> FileReviewResult:
        """
        Perform comprehensive code review combining AI analysis and security scanning.

        Args:
            filename: Name of the file being reviewed
            patch: Git diff patch showing changes
            file_content: Full content of the file

        Returns:
            FileReviewResult with summary, comments, and security issues
        """
        # 1. Run static security scan
        security_issues = self.security_scanner.scan(
            file_content,
            language=RAGSystem._detect_language(filename)
        )

        # 2. Retrieve relevant guidelines using RAG
        guidelines = await self.rag_system.retrieve_guidelines(filename, file_content)

        # 3. Build enhanced prompts with strict output format
        system_prompt = self._build_system_prompt(guidelines)
        user_prompt = self._build_user_prompt(filename, patch, file_content, security_issues)

        # 4. Get AI review with structured output
        response = await self.client.chat.completions.create(
            model=self.deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"}  # Force JSON output
        )

        # 5. Parse and structure the results
        return self._parse_review_response(filename, response, security_issues)

    def _build_system_prompt(self, guidelines: List) -> str:
        """Build system prompt with guidelines and output format specification."""
        guidelines_text = "\n".join([
            f"## {g.title}\n{g.content[:500]}"  # Truncate for token efficiency
            for g in guidelines
        ])

        return f"""You are an expert code reviewer specializing in security, performance, and code quality.

Your task is to perform a thorough code review following these guidelines:

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
      "suggestion": "How to fix it"
    }}
  ],
  "security_issues": [],
  "style_suggestions": ["suggestion1", "suggestion2"]
}}

Be constructive, specific, and actionable. Focus on high-impact issues."""

    # Severity ranking for prompt prioritization (lower number = higher priority).
    _SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

    def _build_user_prompt(
        self,
        filename: str,
        patch: str,
        file_content: str,
        security_issues: List[Dict]
    ) -> str:
        """Build user prompt with code context and pre-scanned security issues."""
        security_context = ""
        if security_issues:
            # Prioritize by severity so the prompt's top-5 budget surfaces the most
            # important findings, not just whatever the regex catalog happened to
            # match first.
            sorted_issues = sorted(
                security_issues,
                key=lambda i: self._SEVERITY_RANK.get(i.get("severity", "LOW"), 99),
            )
            security_context = "\n**Pre-identified Security Issues:**\n"
            for issue in sorted_issues[:5]:  # Top 5 by severity
                security_context += f"- Line {issue['line']}: {issue['description']} ({issue['severity']})\n"

        return f"""**File:** `{filename}`

{security_context}

**Changes (Git Diff):**
```diff
{patch[:2000]}
```

**Full File Content:**
```
{file_content[:4000]}
```

Please provide a comprehensive code review in the specified JSON format."""

    def _parse_review_response(
        self,
        filename: str,
        response,
        security_issues: List[Dict]
    ) -> FileReviewResult:
        """Parse LLM response and construct FileReviewResult."""
        try:
            review_data = json.loads(response.choices[0].message.content)

            # Parse line comments
            line_comments = []
            for comment_data in review_data.get("comments", []):
                line_comments.append(LineComment(
                    line=comment_data.get("line", 1),
                    severity=comment_data.get("severity", "SUGGESTION"),
                    category=comment_data.get("category", "general"),
                    issue=comment_data.get("issue", ""),
                    suggestion=comment_data.get("suggestion", "")
                ))

            # Convert security issues to SecurityIssue objects
            sec_issues = []
            for issue in security_issues:
                sec_issues.append(SecurityIssue(
                    type=issue["type"],
                    severity=issue["severity"],
                    line=issue["line"],
                    description=issue["description"],
                    recommendation=issue["recommendation"]
                ))

            return FileReviewResult(
                filename=filename,
                summary=review_data.get("summary", "Review completed."),
                line_comments=line_comments,
                security_issues=sec_issues,
                style_suggestions=review_data.get("style_suggestions", [])
            )

        except (json.JSONDecodeError, KeyError, AttributeError) as e:
            print(f"Error parsing review response: {e}")
            # Fallback result
            return FileReviewResult(
                filename=filename,
                summary="Review completed with parsing errors.",
                line_comments=[],
                security_issues=[],
                style_suggestions=[]
            )
