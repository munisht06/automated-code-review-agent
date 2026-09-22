import base64
import re
from dataclasses import dataclass
from typing import List, Dict, Any, Set

import httpx

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class PRComment:
    """Represents a comment on a Pull Request."""
    path: str
    line: int
    body: str
    side: str = "RIGHT"  # "LEFT" for old version, "RIGHT" for new version

    def to_review_comment(self) -> Dict[str, Any]:
        """The shape GitHub's create-review endpoint expects for one comment."""
        return {"path": self.path, "line": self.line, "side": self.side, "body": self.body}


def commentable_lines(patch: str) -> Set[int]:
    """New-file line numbers that GitHub accepts review comments on.

    GitHub only accepts line comments on lines that appear in the diff. On
    the new ("RIGHT") side those are added lines and context lines. Each
    hunk's new-side line count, from its header, bounds the lines taken from
    it, so a trailing newline after the last hunk adds no phantom line.
    """
    lines: Set[int] = set()
    new_line = 0
    remaining = 0
    for raw in (patch or "").split("\n"):
        header = _HUNK_HEADER.match(raw)
        if header:
            new_line = int(header.group(1))
            remaining = int(header.group(2)) if header.group(2) is not None else 1
            continue
        if remaining <= 0 or raw.startswith("\\") or raw.startswith("-"):
            continue
        # "+" (added) and " " (context) lines exist in the new file. An empty
        # string inside a hunk is a context line whose leading space was
        # stripped.
        lines.add(new_line)
        new_line += 1
        remaining -= 1
    return lines


class GitHubClient:
    PER_PAGE = 100
    MAX_PAGES = 30  # GitHub returns at most 3,000 files for a pull request

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        self.base_url = "https://api.github.com"

    async def get_pull_request(self, repo: str, pr_number: int) -> Dict[str, Any]:
        url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            data: Dict[str, Any] = response.json()
            return data

    async def get_pr_files(self, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        """All changed files, following pagination (GitHub's default page is 30)."""
        url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}/files"
        files: List[Dict[str, Any]] = []
        async with httpx.AsyncClient() as client:
            for page in range(1, self.MAX_PAGES + 1):
                response = await client.get(
                    url, headers=self.headers, params={"per_page": self.PER_PAGE, "page": page}
                )
                response.raise_for_status()
                batch = response.json()
                files.extend(batch)
                if len(batch) < self.PER_PAGE:
                    break
        return files

    async def get_file_content(self, repo: str, file_path: str, commit_sha: str) -> str:
        url = f"{self.base_url}/repos/{repo}/contents/{file_path}"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=self.headers, params={"ref": commit_sha})
            response.raise_for_status()
            return base64.b64decode(response.json()["content"]).decode("utf-8")

    async def create_pr_comment(self, repo: str, pr_number: int, comment: str):
        url = f"{self.base_url}/repos/{repo}/issues/{pr_number}/comments"
        payload = {"body": comment}
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            response.raise_for_status()

    async def create_pr_review(self, repo: str, pr_number: int, commit_sha: str, comments: List[Dict[str, Any]], summary: str):
        url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}/reviews"
        payload = {
            "commit_id": commit_sha,
            "body": summary,
            "event": "COMMENT",
            "comments": comments,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            response.raise_for_status()
