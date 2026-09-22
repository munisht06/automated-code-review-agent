"""Tests for prompt construction, parsing, retrieval metadata, the scanner's
false-positive guards, diff-line mapping, and webhook input handling."""

import asyncio
import hashlib
import hmac
import json
from unittest.mock import MagicMock

import pytest

from code_review_agent import review_engine as re_mod
from code_review_agent.github_client import commentable_lines
from code_review_agent.rag_system import DEFAULT_GUIDELINES_PATH, GuidelineDocument, RAGSystem
from code_review_agent.review_engine import (
    FileReviewResult,
    LineComment,
    ReviewEngine,
    SecurityScanner,
    clip,
    number_lines,
)


def load_bundled_guidelines():
    rag = RAGSystem()
    asyncio.run(rag._load_guidelines())
    return rag


def response_with(content):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


SCANNER_HIT = [
    {
        "type": "sql_injection",
        "severity": "CRITICAL",
        "line": 10,
        "description": "SQL query with f-string interpolation",
        "recommendation": "Use parameterized queries or prepared statements",
    }
]


# ---- construction without credentials ---------------------------------------


def test_engine_and_rag_construct_without_credentials(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_KEY", raising=False)
    engine = ReviewEngine()
    assert engine.rag_enabled is True
    assert engine.rag_system._client is None  # created lazily, only when used


def test_api_version_is_read_when_the_client_is_created(monkeypatch):
    from code_review_agent.rag_system import azure_api_version

    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2099-01-01")
    assert azure_api_version() == "2099-01-01"
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION")
    assert azure_api_version() == "2024-02-15-preview"


def test_review_records_the_model_the_api_reports():
    from code_review_agent.evaluation.mock_llm import MockAzureClient

    engine = ReviewEngine(client=MockAzureClient())
    content = 'import db\nq = f"SELECT * FROM t WHERE id = {x}"\n'
    result = asyncio.run(engine.review_file("a.py", "", content))
    assert result.response_model == "offline-mock"
    assert result.retrieved_guideline_ids[0] == "python_best_practices"


# ---- the bundled corpus -------------------------------------------------------


class TestCorpus:
    def test_loads_from_package_path_regardless_of_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rag = load_bundled_guidelines()
        assert rag.corpus_source == str(DEFAULT_GUIDELINES_PATH)
        assert {g.id for g in rag.guidelines} == {
            "python_best_practices",
            "typescript_react_standards",
        }

    def test_guideline_language_detected_from_file_name(self):
        rag = load_bundled_guidelines()
        languages = {g.id: g.language for g in rag.guidelines}
        assert languages == {
            "python_best_practices": "python",
            "typescript_react_standards": "typescript",
        }

    def test_missing_corpus_falls_back_visibly(self, tmp_path):
        rag = RAGSystem(guidelines_path=tmp_path / "does-not-exist")
        asyncio.run(rag._load_guidelines())
        assert rag.corpus_source == "built-in defaults"

    def test_every_bundled_guideline_reaches_the_prompt_whole(self):
        rag = load_bundled_guidelines()
        engine = ReviewEngine.__new__(ReviewEngine)
        prompt = engine._build_system_prompt(rag.guidelines)
        for g in rag.guidelines:
            assert len(g.content) <= re_mod.MAX_GUIDELINE_CHARS
            assert g.content in prompt
            assert f"(id: {g.id})" in prompt
        assert "truncated" not in prompt
        assert "## Security" in prompt


# ---- prompt budgets are visible -------------------------------------------------


class TestTruncation:
    def test_clip_marks_the_cut(self):
        text, cut = clip("abcdef", 3)
        assert cut is True
        assert text.startswith("abc") and "truncated: showing 3 of 6 characters" in text
        assert clip("abc", 3) == ("abc", False)

    def test_long_inputs_are_marked_in_prompt_and_recorded(self):
        engine = ReviewEngine.__new__(ReviewEngine)
        long_patch = "+" + "x" * (re_mod.MAX_PATCH_CHARS + 50)
        long_content = "y" * (re_mod.MAX_FILE_CONTENT_CHARS + 50)
        prompt = engine._build_user_prompt("a.py", long_patch, long_content, [])
        assert prompt.count("[... truncated:") == 2
        g = GuidelineDocument(id="big", title="Big", content="z" * (re_mod.MAX_GUIDELINE_CHARS + 1))
        assert "[... truncated:" in engine._build_system_prompt([g])
        cuts = ReviewEngine._prompt_truncations([g], long_patch, long_content)
        assert len(cuts) == 3

    def test_a_long_line_is_cut_at_the_budget(self):
        kept, marker = re_mod.clip_parts("short\n" + "x" * 100, 50)
        assert kept == ("short\n" + "x" * 100)[:50] and marker

    def test_content_cut_at_a_line_break_with_an_unnumbered_marker(self):
        engine = ReviewEngine.__new__(ReviewEngine)
        lines = [f"line{i:04d}" for i in range(1, 1001)]
        prompt = engine._build_user_prompt("a.py", "", "\n".join(lines), [])
        body = prompt.split("line-numbered):**\n```\n", 1)[1].split("\n```", 1)[0].split("\n")
        assert body[-1].startswith("[... truncated:")
        last_numbered = body[-2]
        number, text = last_numbered.split(" | ")
        assert text == f"line{int(number):04d}"  # the last kept line is whole

    def test_file_content_is_line_numbered(self):
        assert number_lines("a\nb") == "   1 | a\n   2 | b"
        engine = ReviewEngine.__new__(ReviewEngine)
        prompt = engine._build_user_prompt("a.py", "", "import db\nx = 1", [])
        assert "   2 | x = 1" in prompt

    def test_no_rag_prompt_says_no_guidelines(self):
        engine = ReviewEngine.__new__(ReviewEngine)
        assert "No guidelines are provided" in engine._build_system_prompt([])


# ---- parsing ---------------------------------------------------------------------


class TestParsing:
    def parse(self, content, scanner=None):
        engine = ReviewEngine.__new__(ReviewEngine)
        return engine._parse_review_response("a.py", response_with(content), scanner or [])

    def test_citations_and_normalization(self):
        r = self.parse(
            json.dumps(
                {
                    "summary": "s",
                    "comments": [
                        {
                            "line": "10",
                            "severity": "critical",
                            "category": "Security",
                            "issue": "i",
                            "suggestion": "s",
                            "cited_guideline_ids": ["python_best_practices"],
                        }
                    ],
                }
            )
        )
        c = r.line_comments[0]
        assert (c.line, c.severity, c.category) == (10, "CRITICAL", "security")
        assert c.cited_guideline_ids == ["python_best_practices"]
        assert r.parse_error is None

    def test_line_values(self):
        r = self.parse(
            json.dumps(
                {
                    "summary": "s",
                    "comments": [
                        {"line": "²", "category": "bug"},
                        {"line": "\u001c5", "category": "bug"},
                        {"line": 0, "category": "bug"},
                        {"line": "-4", "category": "bug"},
                        {"line": 10.0, "category": "bug"},
                        {"line": 10.5, "category": "bug"},
                        {"line": " 7 ", "category": "bug"},
                    ],
                }
            )
        )
        assert [c.line for c in r.line_comments] == [5, 10, 7]
        assert "dropped 4" in r.parse_error

    def test_malformed_comment_dropped_and_recorded(self):
        r = self.parse(
            json.dumps(
                {
                    "summary": "s",
                    "comments": [
                        {"line": "ten", "category": "bug"},
                        {"line": 3, "category": "bug"},
                    ],
                }
            )
        )
        assert [c.line for c in r.line_comments] == [3]
        assert "dropped 1" in r.parse_error

    @pytest.mark.parametrize(
        "content", [None, "not json", "[1, 2]", json.dumps({"comments": None, "summary": "s"})]
    )
    def test_bad_output_keeps_scanner_findings(self, content):
        r = self.parse(content, SCANNER_HIT)
        assert len(r.security_issues) == 1  # scanner evidence survives
        if content != json.dumps({"comments": None, "summary": "s"}):
            assert r.parse_error is not None
            assert r.line_comments == []


# ---- scanner false-positive guards ----------------------------------------------


class TestScannerPrecisionGuards:
    @pytest.mark.parametrize(
        "code",
        [
            "model.eval()",
            "x = ast.literal_eval(s)",
            "m = re.exec(s)",
            'cursor.execute("SELECT a + b FROM t")',
            "profile = get_profile(user_id)",
            "data = read_file(username)",
            'AWS_SECRET_ACCESS_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]',
            'cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))',
        ],
    )
    def test_not_flagged(self, code):
        assert SecurityScanner.scan(code) == []

    @pytest.mark.parametrize(
        "code,vuln",
        [
            ("result = eval(user_input)", "command_injection"),
            ("exec(code)", "command_injection"),
            ("File f = new File(userPath);", "path_traversal"),
            ('aws_secret_access_key = "AKIAABCDEFGHIJKLMNOP"', "hardcoded_secret"),
            ('cursor.execute("SELECT * FROM t WHERE id = " + uid)', "sql_injection"),
            (
                'cursor.execute("SELECT * FROM users WHERE name = \'" + name + "\'")',
                "sql_injection",
            ),
            ('query = "SELECT * FROM users WHERE name = \'" + name', "sql_injection"),
            ('creds = {"aws_secret_access_key": "AKIAABCDEFGHIJKLMNOP"}', "hardcoded_secret"),
            ('os.system(" ".join(args) + " " + user_input)', "command_injection"),
            ('sql = "SELECT " + ", ".join(cols) + " FROM t WHERE id = " + uid', "sql_injection"),
            ('q = "UPDATE " + table + " SET name = \'" + name + "\'"', "sql_injection"),
        ],
    )
    def test_flagged(self, code, vuln):
        assert any(i["type"] == vuln for i in SecurityScanner.scan(code))


# ---- diff line mapping for GitHub review comments ------------------------------


def test_commentable_lines():
    patch = "@@ -3,4 +3,6 @@\n ctx\n-old\n+new1\n+new2\n ctx2\n\\ No newline at end of file"
    assert commentable_lines(patch) == {3, 4, 5, 6}
    assert commentable_lines("") == set()


def test_scanner_ignores_ui_text_and_bounds_long_lines():
    import time

    for code in ('placeholder = "Select " + field', 'title = "Delete " + item.name'):
        assert SecurityScanner.scan(code) == []
    crafted = "\n".join(["aws_secret" * 5000, "open(" + "+" * 50000, ".format(" * 5000])
    start = time.perf_counter()
    SecurityScanner.scan(crafted)
    assert time.perf_counter() - start < 2.0


def test_language_boost_orders_retrieval():
    from code_review_agent import rag_system

    class Embed:
        async def create(self, model, input):
            vec = [0.8, 0.6] if input.startswith("Code review") else [1.0, 0.0]
            if "React" in input:
                vec = [0.9, 0.1]
            return MagicMock(model="m", data=[MagicMock(embedding=vec)])

    rag = RAGSystem(client=MagicMock(embeddings=Embed()))
    ranked = asyncio.run(rag.retrieve_guidelines("a.py", "x = 1"))
    assert [g.id for g in ranked][0] == "python_best_practices"
    rag_system.LANGUAGE_MATCH_BOOST, saved = 1.0, rag_system.LANGUAGE_MATCH_BOOST
    try:
        ranked = asyncio.run(rag.retrieve_guidelines("a.py", "x = 1"))
    finally:
        rag_system.LANGUAGE_MATCH_BOOST = saved
    assert [g.id for g in ranked][0] == "typescript_react_standards"


def test_failed_embedding_call_is_retried_not_half_applied():
    from code_review_agent.evaluation.mock_llm import MockAzureClient

    client = MockAzureClient()
    real_create = client.embeddings.create
    calls = {"n": 0}

    async def flaky(model, input):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated embedding failure")
        return await real_create(model=model, input=input)

    client.embeddings.create = flaky
    rag = RAGSystem(client=client)
    with pytest.raises(RuntimeError):
        asyncio.run(rag.retrieve_guidelines("a.py", "x = 1"))
    ranked = asyncio.run(rag.retrieve_guidelines("a.py", "x = 1"))
    assert {g.id for g in ranked} == {"python_best_practices", "typescript_react_standards"}


def test_commentable_lines_uses_hunk_counts():
    # A trailing newline after the last hunk adds no phantom line.
    assert commentable_lines("@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n") == {1, 2, 3}
    # A blank context line whose leading space was stripped still counts.
    assert commentable_lines("@@ -1,3 +1,3 @@\n a\n\n d") == {1, 2, 3}
    # Two hunks; a header without a count means one line.
    assert commentable_lines("@@ -1 +1 @@\n+x\n@@ -9,2 +10,2 @@\n y\n+z") == {1, 10, 11}


# ---- webhook input handling --------------------------------------------------------


class TestWebhook:
    def client(self, monkeypatch, secret="s3cret"):
        from fastapi.testclient import TestClient

        from code_review_agent import main

        monkeypatch.setattr(main, "GITHUB_WEBHOOK_SECRET", secret)
        monkeypatch.setattr(main, "ALLOW_UNSIGNED_WEBHOOKS", False)
        calls = []

        async def fake_process(*args):
            calls.append(args)

        monkeypatch.setattr(main, "process_pull_request", fake_process)
        return TestClient(main.app), calls

    @staticmethod
    def sign(body, secret="s3cret"):
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def post(self, client, body, sig):
        return client.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
            },
        )

    def test_bad_signature_401(self, monkeypatch):
        client, calls = self.client(monkeypatch)
        assert self.post(client, b"{}", "sha256=bad").status_code == 401
        assert calls == []

    def test_malformed_json_400(self, monkeypatch):
        client, _ = self.client(monkeypatch)
        body = b"{not json"
        assert self.post(client, body, self.sign(body)).status_code == 400

    def test_missing_fields_400(self, monkeypatch):
        client, _ = self.client(monkeypatch)
        body = json.dumps({"action": "opened"}).encode()
        assert self.post(client, body, self.sign(body)).status_code == 400

    def test_valid_event_queued(self, monkeypatch):
        client, calls = self.client(monkeypatch)
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "pull_request": {"number": 7, "head": {"sha": "abc"}},
            }
        ).encode()
        resp = self.post(client, body, self.sign(body))
        assert resp.status_code == 200
        assert resp.json() == {"status": "processing", "pr_number": 7}
        assert calls == [("o/r", 7, "abc")]


# ---- the review loop in process_pull_request ------------------------------------


def test_process_pull_request_routes_comments(monkeypatch):
    from code_review_agent import github_client, main
    from code_review_agent import review_engine as engine_mod

    patch = "@@ -1,2 +1,3 @@\n a\n+b\n c"
    posted = {}

    class FakeGitHub:
        def __init__(self, token):
            pass

        async def get_pr_files(self, repo, pr_number):
            return [
                {"filename": "a.py", "status": "modified", "patch": patch},
                {"filename": "b.py", "status": "modified", "patch": patch},
                {"filename": "gone.py", "status": "removed"},
                {"filename": "README.md", "status": "modified", "patch": patch},
            ]

        async def get_file_content(self, repo, path, sha):
            return "a\nb\nc\n"

        async def create_pr_review(self, repo, pr_number, commit_sha, comments, summary):
            posted["comments"], posted["summary"] = comments, summary

        async def create_pr_comment(self, repo, pr_number, body):
            posted["error_comment"] = body

    class FakeEngine:
        async def review_file(self, filename, patch, file_content):
            if filename == "b.py":
                raise RuntimeError("secret detail")
            return FileReviewResult(
                filename=filename,
                summary="ok",
                line_comments=[
                    LineComment(
                        line=2, severity="WARNING", category="bug", issue="in diff", suggestion="s"
                    ),
                    LineComment(
                        line=40, severity="WARNING", category="bug", issue="outside", suggestion="s"
                    ),
                ],
            )

    monkeypatch.setattr(github_client, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(engine_mod, "ReviewEngine", FakeEngine)
    asyncio.run(main.process_pull_request("o/r", 1, "sha"))

    assert "error_comment" not in posted
    assert [(c["path"], c["line"], c["side"]) for c in posted["comments"]] == [("a.py", 2, "RIGHT")]
    assert "`a.py` line 40: outside" in posted["summary"]
    assert "Files not reviewed" in posted["summary"] and "`b.py`" in posted["summary"]
    assert "secret detail" not in posted["summary"]
    assert "gone.py" not in posted["summary"]


def test_summary_counts_what_it_leaves_out():
    from code_review_agent.main import generate_review_summary

    text = generate_review_summary(
        ["ok"],
        outside_diff=[f"c{i}" for i in range(25)],
        style_suggestions=[f"s{i}" for i in range(12)],
    )
    assert "...and 5 more" in text and "...and 2 more" in text
