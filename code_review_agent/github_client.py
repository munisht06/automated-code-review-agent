import httpx
from dataclasses import dataclass
from typing import List, Dict, Any


@dataclass
class PRComment:
    """Represents a comment on a Pull Request."""
    path: str
    line: int
    body: str
    side: str = "RIGHT"  # "LEFT" for old version, "RIGHT" for new version


class GitHubClient:
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
            return response.json()

    async def get_pr_files(self, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}/files"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def get_file_content(self, repo: str, file_path: str, commit_sha: str) -> str:
        url = f"{self.base_url}/repos/{repo}/contents/{file_path}?ref={commit_sha}"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            # Assuming the content is base64 encoded
            import base64
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
