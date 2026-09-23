"""
Automated Code Review Agent: FastAPI service that receives GitHub
pull-request webhooks and posts a retrieval-grounded LLM review.
"""

import hashlib
import hmac
import json
import logging
import os
import re

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

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

    expected = (
        "sha256=" + hmac.new(GITHUB_WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    )

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
            f for f in files if f.get("status") != "removed" and is_reviewable_file(f["filename"])
        ]

        if not reviewable_files:
            await github.create_pr_comment(
                repo, pr_number, "✅ No code files to review in this PR."
            )
            return

        all_comments = []
        outside_diff = []
        summary_points = []
        all_security_issues = []
        all_style_suggestions: list[str] = []
        skipped_files = []
        unreadable_files = []

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

            # A response that parsed but lost a malformed comment is not an
            # unreadable response: dropped_comments records that separately.
            if review_result.parse_error and not review_result.dropped_comments:
                unreadable_files.append(filename)

            # GitHub rejects the whole review if any comment targets a line
            # outside the diff, so those comments go into the summary instead.
            allowed = commentable_lines(patch)
            for comment in review_result.line_comments:
                body = (
                    f"**[{sanitize_model_text(comment.severity, 20)}] "
                    f"{sanitize_model_text(comment.category, 20).upper()}**\n\n"
                    f"**Issue:** {sanitize_model_text(comment.issue)}\n\n"
                    f"**Suggestion:** {sanitize_model_text(comment.suggestion)}"
                )
                if comment.line in allowed:
                    all_comments.append(
                        PRComment(path=filename, line=comment.line, body=body).to_review_comment()
                    )
                else:
                    outside_diff.append(
                        f"`{filename}` line {comment.line}: "
                        f"{sanitize_model_text(comment.issue)}"
                    )

            all_security_issues.extend(review_result.security_issues)
            if review_result.summary:
                summary_points.append(f"`{filename}`: {sanitize_model_text(review_result.summary)}")
            all_style_suggestions.extend(
                sanitize_model_text(s) for s in review_result.style_suggestions
            )

        await github.create_pr_review(
            repo=repo,
            pr_number=pr_number,
            commit_sha=commit_sha,
            comments=all_comments,
            summary=generate_review_summary(
                summary_points,
                all_security_issues,
                all_style_suggestions,
                outside_diff=outside_diff,
                skipped_files=skipped_files,
                unreadable_files=unreadable_files,
            ),
        )

    except Exception:
        # Details go to the service log, not to a public PR comment.
        logger.exception("Error processing %s#%s", repo, pr_number)
        await github.create_pr_comment(
            repo,
            pr_number,
            "⚠️ Code review encountered an error and could not complete. "
            "See the service logs for details.",
        )


def is_reviewable_file(filename: str) -> bool:
    """Check if file should be reviewed based on extension."""
    reviewable_extensions = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
    }
    return any(filename.endswith(ext) for ext in reviewable_extensions)


# Model output is derived from pull-request content, which anyone can write.
# It is posted into a public review body, so it is flattened to one line and
# the characters that would let it break out of its bullet, forge a heading,
# or embed an image or link are neutralized before it is rendered.
_MARKDOWN_BREAKOUT = re.compile(r"^[\s>#*_+=-]+|[\s>#*_+=-]+$")
MAX_MODEL_TEXT_CHARS = 500


def sanitize_model_text(text: str, limit: int = MAX_MODEL_TEXT_CHARS) -> str:
    """Make model-supplied text safe to render inside a review body.

    GitHub renders a safelist of HTML inside markdown, so ``<`` is escaped
    too: an ``<img>`` in a public review body is a tracking pixel aimed at
    everyone who opens the pull request.
    """
    flattened = " ".join(str(text).split())
    flattened = _MARKDOWN_BREAKOUT.sub("", flattened)
    flattened = (
        flattened.replace("<", "&lt;").replace("![", "! [").replace("](", "] (").replace("`", "'")
    )
    if len(flattened) > limit:
        flattened = flattened[:limit].rstrip() + "..."
    return flattened


# Severity order for listing scanner findings, and how many are listed.
_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
MAX_SCANNER_FINDINGS_IN_SUMMARY = 20


def generate_review_summary(
    summaries: list[str],
    security_issues: list | None = None,
    style_suggestions: list[str] | None = None,
    outside_diff: list[str] | None = None,
    skipped_files: list[str] | None = None,
    unreadable_files: list[str] | None = None,
) -> str:
    """Build the review summary: scanner findings, per-file notes, comments that
    fall outside the diff, style suggestions, and files that could not be
    reviewed. Long lists are cut with a count of what was left out."""
    summary_text = "## 🤖 AI Code Review Summary\n\n"

    if security_issues:
        # Every scanner hit is listed with its location and the pattern's
        # description: a count alone cannot be audited or acted on.
        ranked = sorted(
            security_issues,
            key=lambda i: (_SEVERITY_ORDER.get(i.severity, 99), i.file, i.line),
        )
        summary_text += "### 🚨 Static scanner findings\n\n"
        for issue in ranked[:MAX_SCANNER_FINDINGS_IN_SUMMARY]:
            where = f"`{issue.file}` line {issue.line}" if issue.file else f"line {issue.line}"
            summary_text += (
                f"- **[{issue.severity}] {issue.type}** {where}: "
                f"{issue.description}. {issue.recommendation}\n"
            )
        if len(ranked) > MAX_SCANNER_FINDINGS_IN_SUMMARY:
            summary_text += f"- ...and {len(ranked) - MAX_SCANNER_FINDINGS_IN_SUMMARY} more\n"
        summary_text += "\n"

    summary_text += "### 📝 Review Notes\n\n"
    summary_text += "\n".join(f"- {s}" for s in summaries if s)

    if outside_diff:
        summary_text += "\n\n### 📍 Comments on lines outside the diff\n\n"
        summary_text += "\n".join(f"- {c}" for c in outside_diff[:20])
        if len(outside_diff) > 20:
            summary_text += f"\n- ...and {len(outside_diff) - 20} more"

    if style_suggestions:
        unique = []
        seen = set()
        for s in style_suggestions:
            if s and s not in seen:
                unique.append(s)
                seen.add(s)
        if unique:
            summary_text += "\n\n### 💡 Style Suggestions\n\n"
            summary_text += "\n".join(f"- {s}" for s in unique[:10])
            if len(unique) > 10:
                summary_text += f"\n- ...and {len(unique) - 10} more"

    if unreadable_files:
        summary_text += "\n\n### 🧩 Files whose model response could not be read\n\n"
        summary_text += "\n".join(f"- `{f}`" for f in unreadable_files)

    if skipped_files:
        summary_text += "\n\n### ⏭️ Files not reviewed\n\n"
        summary_text += "\n".join(f"- `{f}`" for f in skipped_files)

    summary_text += (
        "\n\n---\n*This review was generated by Code Review Agent using Azure AI and RAG.*"
    )

    return summary_text


@app.get("/health")
async def health_check():
    """Liveness, plus whether the service is configured. The booleans say only
    that a value is set, never what it is. A deployment with no Azure
    credentials answers every webhook with an error, and one with neither a
    webhook secret nor ALLOW_UNSIGNED_WEBHOOKS rejects every delivery with
    401; both should be visible here rather than on the first pull
    request."""
    return {
        "status": "healthy",
        "service": "code_review_agent",
        "azure_openai_configured": bool(
            os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_KEY")
        ),
        "github_token_configured": bool(GITHUB_TOKEN),
        "webhook_secret_configured": bool(GITHUB_WEBHOOK_SECRET),
        "unsigned_webhooks_allowed": bool(ALLOW_UNSIGNED_WEBHOOKS),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
