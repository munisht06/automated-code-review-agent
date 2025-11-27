"""
Automated Code Review Agent
An intelligent AI agent that automates code review using Azure OpenAI and RAG techniques.
"""

import os
import hmac
import hashlib
import json
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from pydantic import BaseModel
import httpx
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Code Review Agent", version="1.0.0")

# Configuration
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")

# GitHub API base URL
GITHUB_API = "https://api.github.com"


class WebhookPayload(BaseModel):
    action: str
    number: int
    pull_request: dict
    repository: dict


def verify_github_signature(payload: bytes, signature: str) -> bool:
    """Verify the GitHub webhook signature for security."""
    if not GITHUB_WEBHOOK_SECRET:
        return True  # Skip verification in development
    
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected, signature)


@app.post("/webhook/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle incoming GitHub webhook events for Pull Requests."""
    payload = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    
    if not verify_github_signature(payload, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    
    event_type = request.headers.get("X-GitHub-Event")
    if event_type != "pull_request":
        return {"status": "ignored", "reason": "Not a PR event"}
    
    data = json.loads(payload)
    action = data.get("action")
    
    # Process only opened or synchronized (new commits) PRs
    if action not in ["opened", "synchronize"]:
        return {"status": "ignored", "reason": f"Action {action} not processed"}
    
    # Queue the review process in background
    background_tasks.add_task(
        process_pull_request,
        data["repository"]["full_name"],
        data["pull_request"]["number"],
        data["pull_request"]["head"]["sha"]
    )
    
    return {"status": "processing", "pr_number": data["pull_request"]["number"]}


async def process_pull_request(repo: str, pr_number: int, commit_sha: str):
    """Main orchestration function for PR review."""
    from code_review_agent.github_client import GitHubClient
    from code_review_agent.review_engine import ReviewEngine
    
    github = GitHubClient(GITHUB_TOKEN)
    review_engine = ReviewEngine()
    
    try:
        # 1. Fetch PR diff and file contents
        pr_data = await github.get_pull_request(repo, pr_number)
        files = await github.get_pr_files(repo, pr_number)
        
        # 2. Filter reviewable files
        reviewable_files = [f for f in files if is_reviewable_file(f["filename"])]
        
        if not reviewable_files:
            await github.create_pr_comment(
                repo, pr_number,
                "✅ No code files to review in this PR."
            )
            return
        
        # 3. Generate AI-powered review for each file
        all_comments = []
        summary_points = []
        all_security_issues = []

        for file_info in reviewable_files:
            review_result = await review_engine.review_file(
                filename=file_info["filename"],
                patch=file_info.get("patch", ""),
                file_content=await github.get_file_content(
                    repo, file_info["filename"], commit_sha
                )
            )

            # Convert LineComment objects to GitHub API format
            for comment in review_result.line_comments:
                all_comments.append({
                    "path": review_result.filename,
                    "position": comment.line,
                    "body": f"**[{comment.severity}] {comment.category.upper()}**\n\n"
                            f"**Issue:** {comment.issue}\n\n"
                            f"**Suggestion:** {comment.suggestion}"
                })

            # Track security issues separately
            all_security_issues.extend(review_result.security_issues)
            summary_points.append(review_result.summary)

        # 4. Post review to GitHub
        await github.create_pr_review(
            repo=repo,
            pr_number=pr_number,
            commit_sha=commit_sha,
            comments=all_comments,
            summary=generate_review_summary(summary_points, all_security_issues)
        )
        
    except Exception as e:
        print(f"Error processing PR #{pr_number}: {e}")
        await github.create_pr_comment(
            repo, pr_number,
            f"⚠️ Code review encountered an error: {str(e)}"
        )


def is_reviewable_file(filename: str) -> bool:
    """Check if file should be reviewed based on extension."""
    reviewable_extensions = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs",
        ".go", ".rs", ".rb", ".php", ".swift", ".kt"
    }
    return any(filename.endswith(ext) for ext in reviewable_extensions)


def generate_review_summary(summaries: list[str], security_issues: list = None) -> str:
    """Generate overall review summary with security alerts."""
    summary_text = "## 🤖 AI Code Review Summary\n\n"

    # Add security alerts if any
    if security_issues:
        critical = [i for i in security_issues if i.severity == "CRITICAL"]
        high = [i for i in security_issues if i.severity == "HIGH"]

        if critical or high:
            summary_text += "### 🚨 Security Alerts\n\n"
            if critical:
                summary_text += f"- **{len(critical)} CRITICAL** security issue(s) found\n"
            if high:
                summary_text += f"- **{len(high)} HIGH** severity issue(s) found\n"
            summary_text += "\n"

    # Add file summaries
    summary_text += "### 📝 Review Notes\n\n"
    summary_text += chr(10).join(f'- {s}' for s in summaries if s)

    summary_text += "\n\n---\n*This review was generated by Code Review Agent using Azure AI and RAG.*"

    return summary_text


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "code_review_agent"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)