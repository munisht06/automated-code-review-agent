"""
Automated Code Review Agent: FastAPI service that receives GitHub
pull-request webhooks and posts a retrieval-grounded LLM review.
"""

import os
import hmac
import hashlib
import json
import logging
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from dotenv import load_dotenv

from code_review_agent.github_client import PRComment, commentable_lines

load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI(title="Code Review Agent", version="0.1.0")

# Configuration
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
# Unsigned webhooks are rejected unless explicitly allowed for local development.
ALLOW_UNSIGNED_WEBHOOKS = os.getenv("ALLOW_UNSIGNED_WEBHOOKS", "").lower() in {"1", "true", "yes"}


def verify_github_signature(payload: bytes, signature: str) -> bool:
    """Verify the GitHub webhook HMAC-SHA256 signature.

    Fails closed: with no secret configured, every request is rejected unless
    ALLOW_UNSIGNED_WEBHOOKS is set for local development.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return ALLOW_UNSIGNED_WEBHOOKS

    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    try:
        return hmac.compare_digest(expected, signature)
    except TypeError:
        # compare_digest rejects non-ASCII str input; treat as a bad signature.
        return False


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

    try:
        data = json.loads(payload)
        action = data.get("action")
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError) as err:
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from err

    # Process only opened or synchronized (new commits) PRs
    if action not in ["opened", "synchronize"]:
        return {"status": "ignored", "reason": f"Action {action} not processed"}

    try:
        repo = data["repository"]["full_name"]
        pr_number = data["pull_request"]["number"]
        head_sha = data["pull_request"]["head"]["sha"]
    except (KeyError, TypeError) as err:
        raise HTTPException(status_code=400, detail="Payload missing pull request fields") from err

    # Queue the review and return immediately (HTTP 200 with a status body).
    background_tasks.add_task(process_pull_request, repo, pr_number, head_sha)
    return {"status": "processing", "pr_number": pr_number}


async def process_pull_request(repo: str, pr_number: int, commit_sha: str):
    """Main orchestration function for PR review."""
    from code_review_agent.github_client import GitHubClient
    from code_review_agent.review_engine import ReviewEngine

    github = GitHubClient(GITHUB_TOKEN or "")

    try:
        review_engine = ReviewEngine()
        files = await github.get_pr_files(repo, pr_number)

        # Removed files have no content at the head commit.
        reviewable_files = [
            f for f in files
            if f.get("status") != "removed" and is_reviewable_file(f["filename"])
        ]

        if not reviewable_files:
            await github.create_pr_comment(
                repo, pr_number,
                "✅ No code files to review in this PR."
            )
            return

        all_comments = []
        outside_diff = []
        summary_points = []
        all_security_issues = []
        all_style_suggestions = []
        skipped_files = []

        for file_info in reviewable_files:
            filename = file_info["filename"]
            patch = file_info.get("patch", "")
            # One failing file (too large, binary, deleted mid-review) should
            # not abort the review of the others.
            try:
                review_result = await review_engine.review_file(
                    filename=filename,
                    patch=patch,
                    file_content=await github.get_file_content(repo, filename, commit_sha),
                )
            except Exception:
                logger.exception("Review failed for %s in %s#%s", filename, repo, pr_number)
                skipped_files.append(filename)
                continue

            # GitHub rejects the whole review if any comment targets a line
            # outside the diff, so those comments go into the summary instead.
            allowed = commentable_lines(patch)
            for comment in review_result.line_comments:
                body = (
                    f"**[{comment.severity}] {comment.category.upper()}**\n\n"
                    f"**Issue:** {comment.issue}\n\n"
                    f"**Suggestion:** {comment.suggestion}"
                )
                if comment.line in allowed:
                    all_comments.append(
                        PRComment(path=filename, line=comment.line, body=body).to_review_comment()
                    )
                else:
                    outside_diff.append(f"`{filename}` line {comment.line}: {comment.issue}")

            all_security_issues.extend(review_result.security_issues)
            summary_points.append(review_result.summary)
            all_style_suggestions.extend(review_result.style_suggestions)

        await github.create_pr_review(
            repo=repo,
            pr_number=pr_number,
            commit_sha=commit_sha,
            comments=all_comments,
            summary=generate_review_summary(
                summary_points, all_security_issues, all_style_suggestions,
                outside_diff=outside_diff, skipped_files=skipped_files,
            )
        )

    except Exception:
        # Details go to the service log, not to a public PR comment.
        logger.exception("Error processing %s#%s", repo, pr_number)
        await github.create_pr_comment(
            repo, pr_number,
            "⚠️ Code review encountered an error and could not complete. "
            "See the service logs for details."
        )


def is_reviewable_file(filename: str) -> bool:
    """Check if file should be reviewed based on extension."""
    reviewable_extensions = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs",
        ".go", ".rs", ".rb", ".php", ".swift", ".kt"
    }
    return any(filename.endswith(ext) for ext in reviewable_extensions)


def generate_review_summary(
    summaries: list[str],
    security_issues: list | None = None,
    style_suggestions: list[str] | None = None,
    outside_diff: list[str] | None = None,
    skipped_files: list[str] | None = None,
) -> str:
    """Generate overall review summary with security alerts and style suggestions."""
    summary_text = "## 🤖 AI Code Review Summary\n\n"

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

    summary_text += "### 📝 Review Notes\n\n"
    summary_text += chr(10).join(f'- {s}' for s in summaries if s)

    if outside_diff:
        summary_text += "\n\n### 📍 Comments on lines outside the diff\n\n"
        summary_text += chr(10).join(f"- {c}" for c in outside_diff[:20])

    if style_suggestions:
        unique = []
        seen = set()
        for s in style_suggestions:
            if s and s not in seen:
                unique.append(s)
                seen.add(s)
        if unique:
            summary_text += "\n\n### 💡 Style Suggestions\n\n"
            summary_text += chr(10).join(f"- {s}" for s in unique[:10])

    if skipped_files:
        summary_text += "\n\n### ⏭️ Files not reviewed\n\n"
        summary_text += chr(10).join(f"- `{f}`" for f in skipped_files)

    summary_text += "\n\n---\n*This review was generated by Code Review Agent using Azure AI and RAG.*"

    return summary_text


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "code_review_agent"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
