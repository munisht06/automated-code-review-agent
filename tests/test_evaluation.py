"""Tests for the evaluation harness: metrics and an offline end-to-end run."""

import json
from pathlib import Path

import pytest

from code_review_agent.evaluation.fixtures import (
    ExpectedIssue,
    Fixture,
    FixtureFile,
    NegativeAssertion,
    load_fixture,
)
from code_review_agent.evaluation.metrics import (
    compute_citation_checks,
    compute_consistency,
    compute_correctness,
    emit_grounding_tasks,
    summarize_correctness,
)
from code_review_agent.evaluation.runner import main as runner_main
from code_review_agent.review_engine import FileReviewResult, LineComment

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "prs" / "py-sql-injection-001.json"


def comment(line, category="security", severity="CRITICAL", cited=None):
    return LineComment(
        line=line,
        severity=severity,
        category=category,
        issue="x",
        suggestion="y",
        cited_guideline_ids=list(cited or []),
    )


def result(filename, comments, retrieved=None, parse_error=None):
    return FileReviewResult(
        filename=filename,
        summary="",
        line_comments=list(comments),
        retrieved_guideline_ids=list(retrieved or []),
        parse_error=parse_error,
    )


def make_fixture(expected, negatives=None, files=("a.py",)):
    return Fixture(
        fixture_id="t",
        language="python",
        description="",
        files=[FixtureFile(path=f, language="python", patch="", content="") for f in files],
        expected_issues=list(expected),
        negative_assertions=list(negatives or []),
    )


# ---- correctness ------------------------------------------------------------


class TestCorrectness:
    def test_duplicate_comments_do_not_inflate_recall(self):
        fx = make_fixture(
            [
                ExpectedIssue(file="a.py", line=10, category="security", severity="CRITICAL"),
                ExpectedIssue(file="a.py", line=40, category="security", severity="CRITICAL"),
            ]
        )
        m = compute_correctness(fx, [result("a.py", [comment(10), comment(10), comment(11)])])
        assert m.recall == pytest.approx(0.5)
        assert m.true_positives == 1
        assert m.false_positives == 2
        assert m.duplicate_findings == 2
        assert m.precision == pytest.approx(1 / 3)

    def test_interleaved_files_credit_the_right_issue(self):
        fx = make_fixture(
            [
                ExpectedIssue(file="a.py", line=10, category="security", severity="CRITICAL"),
                ExpectedIssue(file="b.py", line=20, category="security", severity="CRITICAL"),
                ExpectedIssue(file="a.py", line=30, category="security", severity="CRITICAL"),
            ],
            files=("a.py", "b.py"),
        )
        m = compute_correctness(fx, [result("a.py", []), result("b.py", [comment(20)])])
        assert m.true_positives == 1
        assert [(e.file, e.line) for _c, e in m.matched_pairs] == [("b.py", 20)]
        assert {(e.file, e.line) for e in m.unmatched_expected} == {("a.py", 10), ("a.py", 30)}

    def test_category_match_is_case_insensitive(self):
        fx = make_fixture(
            [ExpectedIssue(file="a.py", line=10, category="security", severity="CRITICAL")]
        )
        m = compute_correctness(fx, [result("a.py", [comment(10, category="Security")])])
        assert m.true_positives == 1

    def test_line_tolerance(self):
        fx = make_fixture(
            [ExpectedIssue(file="a.py", line=10, category="security", severity="CRITICAL")]
        )
        assert compute_correctness(fx, [result("a.py", [comment(13)])]).true_positives == 1
        assert compute_correctness(fx, [result("a.py", [comment(14)])]).true_positives == 0
        assert (
            compute_correctness(
                fx, [result("a.py", [comment(14)])], line_tolerance=4
            ).true_positives
            == 1
        )

    def test_clean_fixture_rates_are_undefined_not_zero(self):
        fx = make_fixture([])
        m = compute_correctness(fx, [result("a.py", [])])
        assert m.precision is None and m.recall is None and m.f1 is None
        assert m.severity_weighted_recall is None
        m2 = compute_correctness(fx, [result("a.py", [comment(5)])])
        assert m2.false_positives == 1
        assert m2.precision == 0.0
        assert m2.recall is None

    def test_line_scoped_negative_assertion(self):
        fx = make_fixture(
            [ExpectedIssue(file="a.py", line=10, category="security", severity="CRITICAL")],
            negatives=[
                NegativeAssertion(file="a.py", category="security", line=18, line_tolerance=3)
            ],
        )
        m = compute_correctness(fx, [result("a.py", [comment(10), comment(18), comment(40)])])
        assert m.true_positives == 1
        assert len(m.negative_assertion_violations) == 1
        assert m.negative_assertion_violations[0].comment.line == 18

    def test_summary_across_runs(self):
        fx = make_fixture(
            [
                ExpectedIssue(file="a.py", line=10, category="security", severity="CRITICAL"),
                ExpectedIssue(file="a.py", line=40, category="security", severity="CRITICAL"),
            ]
        )
        runs = [[result("a.py", [comment(10), comment(40)])], [result("a.py", [comment(10)])]]
        s = summarize_correctness([compute_correctness(fx, r) for r in runs])
        assert s["runs"] == 2
        assert s["recall"]["per_run"] == [1.0, 0.5]
        assert s["recall"]["mean"] == pytest.approx(0.75)
        assert s["recall"]["min"] == 0.5 and s["recall"]["max"] == 1.0

    def test_maximum_matching_not_greedy(self):
        # Greedy closest-first would pair 11 with 10 and leave 8 unmatched.
        fx = make_fixture(
            [
                ExpectedIssue(file="a.py", line=10, category="security", severity="CRITICAL"),
                ExpectedIssue(file="a.py", line=14, category="security", severity="CRITICAL"),
            ]
        )
        m = compute_correctness(fx, [result("a.py", [comment(11), comment(8)])])
        assert (m.true_positives, m.false_positives, m.duplicate_findings) == (2, 0, 0)

    def test_empty_run_scores_f1_zero_not_undefined(self):
        fx = make_fixture(
            [ExpectedIssue(file="a.py", line=10, category="security", severity="CRITICAL")]
        )
        found = compute_correctness(fx, [result("a.py", [comment(10)])])
        empty = compute_correctness(fx, [result("a.py", [])])
        assert empty.precision is None and empty.recall == 0.0 and empty.f1 == 0.0
        s = summarize_correctness([found, empty])
        assert s["f1"]["mean"] == pytest.approx(0.5)
        assert s["precision"]["n_defined"] == 1
        assert s["pooled"]["recall"] == pytest.approx(0.5)
        assert s["pooled"]["precision"] == pytest.approx(1.0)

    def test_severity_weight_is_case_insensitive(self):
        fx = make_fixture(
            [
                ExpectedIssue(file="a.py", line=10, category="security", severity="critical"),
                ExpectedIssue(file="a.py", line=40, category="style", severity="SUGGESTION"),
            ]
        )
        m = compute_correctness(fx, [result("a.py", [comment(10)])])
        assert m.severity_weighted_recall == pytest.approx(5 / 6)


# ---- consistency ------------------------------------------------------------


class TestConsistency:
    def test_one_line_jitter_is_the_same_finding(self):
        runs = [[result("a.py", [comment(10)])], [result("a.py", [comment(11)])]]
        c = compute_consistency(runs)
        assert c.mean_jaccard == pytest.approx(1.0)
        assert c.severity_stability == pytest.approx(1.0)

    def test_jitter_beyond_tolerance_is_a_different_finding(self):
        runs = [[result("a.py", [comment(10)])], [result("a.py", [comment(20)])]]
        c = compute_consistency(runs)
        assert c.mean_jaccard == 0.0
        assert c.severity_stability is None

    def test_parse_failures_are_excluded_not_scored_as_agreement(self):
        failed = [result("a.py", [], parse_error="JSONDecodeError")]
        runs = [failed, failed, [result("a.py", [comment(10)])]]
        c = compute_consistency(runs)
        assert c.excluded_parse_failures == 2
        assert c.runs_scored == 1
        assert c.mean_jaccard is None

    def test_severity_change_lowers_stability(self):
        runs = [
            [result("a.py", [comment(10, severity="CRITICAL")])],
            [result("a.py", [comment(10, severity="WARNING")])],
        ]
        c = compute_consistency(runs)
        assert c.mean_jaccard == pytest.approx(1.0)
        assert c.severity_stability == 0.0

    def test_partial_overlap(self):
        runs = [
            [result("a.py", [comment(10), comment(30)])],
            [result("a.py", [comment(10)])],
        ]
        c = compute_consistency(runs)
        assert c.mean_jaccard == pytest.approx(0.5)

    def test_maximum_matching_across_runs(self):
        runs = [
            [result("a.py", [comment(8), comment(11)])],
            [result("a.py", [comment(10), comment(14)])],
        ]
        assert compute_consistency(runs).mean_jaccard == pytest.approx(1.0)

    def test_tied_findings_do_not_depend_on_order(self):
        a = [comment(10, severity="WARNING"), comment(10, severity="CRITICAL")]
        b = [comment(10, severity="CRITICAL"), comment(10, severity="WARNING")]
        for first, second in ((a, b), (a, a), (b, a)):
            c = compute_consistency([[result("a.py", first)], [result("a.py", second)]])
            assert c.severity_stability == pytest.approx(1.0)

    def test_scores_do_not_depend_on_finding_order(self):
        from itertools import permutations

        fx = make_fixture(
            [
                ExpectedIssue(file="a.py", line=10, category="security", severity="CRITICAL"),
                ExpectedIssue(file="a.py", line=12, category="security", severity="CRITICAL"),
                ExpectedIssue(file="a.py", line=12, category="security", severity="SUGGESTION"),
            ],
            negatives=[
                NegativeAssertion(file="a.py", category="security", line=12, line_tolerance=1)
            ],
        )
        scores = set()
        for order in permutations([8, 7, 15, 11]):
            m = compute_correctness(fx, [result("a.py", [comment(n) for n in order])])
            scores.add((m.severity_weighted_recall, len(m.negative_assertion_violations)))
        assert len(scores) == 1
        stabilities = {
            compute_consistency(
                [
                    [result("a.py", [comment(n) for n in base])],
                    [result("a.py", [comment(10)])],
                    [result("a.py", [comment(12)])],
                ]
            ).severity_stability
            for base in ([7, 13], [13, 7])
        }
        assert len(stabilities) == 1

    def test_severity_stability_is_one_to_one(self):
        # Only the finding at 10 is the same finding as 11 in the other run.
        runs = [
            [result("a.py", [comment(10, severity="CRITICAL"), comment(13, severity="WARNING")])],
            [result("a.py", [comment(11, severity="CRITICAL")])],
        ]
        assert compute_consistency(runs).severity_stability == pytest.approx(1.0)

    def test_failed_calls_are_excluded(self):
        failed = [FileReviewResult(filename="a.py", summary="", call_error="APIError: boom")]
        runs = [failed, [result("a.py", [comment(10)])], [result("a.py", [comment(10)])]]
        c = compute_consistency(runs)
        assert (c.excluded_call_failures, c.excluded_parse_failures, c.runs_scored) == (1, 0, 2)


# ---- citation checks and grounding tasks ------------------------------------


class TestCitation:
    def fixture(self):
        return make_fixture(
            [
                ExpectedIssue(
                    file="a.py",
                    line=10,
                    category="security",
                    severity="CRITICAL",
                    must_cite=["python_best_practices"],
                ),
            ]
        )

    def test_retrieved_and_cited(self):
        fx = self.fixture()
        run = [
            result(
                "a.py",
                [comment(10, cited=["python_best_practices"])],
                retrieved=["python_best_practices", "typescript_react_standards"],
            )
        ]
        checks = compute_citation_checks(fx, run, compute_correctness(fx, run))
        assert checks.retrieved_rate == 1.0
        assert checks.cited_rate == 1.0

    def test_tied_findings_prefer_the_citing_one(self):
        fx = self.fixture()
        citing = comment(10, cited=["python_best_practices"])
        for comments in ([citing, comment(10)], [comment(10), citing]):
            run = [result("a.py", comments, retrieved=["python_best_practices"])]
            assert compute_citation_checks(fx, run, compute_correctness(fx, run)).cited_rate == 1.0

    def test_retrieved_but_not_cited_is_a_grounding_miss(self):
        fx = self.fixture()
        run = [result("a.py", [comment(10)], retrieved=["python_best_practices"])]
        checks = compute_citation_checks(fx, run, compute_correctness(fx, run))
        assert checks.retrieved_rate == 1.0
        assert checks.cited_rate == 0.0

    def test_not_retrieved_is_a_retrieval_miss(self):
        fx = self.fixture()
        run = [result("a.py", [comment(10)], retrieved=[])]
        checks = compute_citation_checks(fx, run, compute_correctness(fx, run))
        assert checks.retrieved_rate == 0.0

    def test_unmatched_issue_has_no_cited_rate(self):
        fx = self.fixture()
        run = [result("a.py", [], retrieved=["python_best_practices"])]
        checks = compute_citation_checks(fx, run, compute_correctness(fx, run))
        assert checks.cited_rate is None

    def test_grounding_tasks_carry_ids(self):
        fx = self.fixture()
        run = [
            result(
                "a.py",
                [comment(10, cited=["python_best_practices"])],
                retrieved=["python_best_practices"],
            )
        ]
        tasks = emit_grounding_tasks(fx, run)
        assert len(tasks) == 1
        assert tasks[0].cited_guideline_ids == ["python_best_practices"]
        assert tasks[0].retrieved_guideline_ids == ["python_best_practices"]


# ---- the bundled fixture -----------------------------------------------------


class TestBundledFixture:
    def test_hunk_header_agrees_with_content(self):
        from code_review_agent.github_client import commentable_lines

        fx = load_fixture(FIXTURE_PATH)
        ff = fx.files[0]
        expected = fx.expected_issues[0]
        # The labeled line is inside the diff and holds the vulnerable query.
        assert expected.line in commentable_lines(ff.patch)
        assert 'query = f"SELECT' in ff.content.split("\n")[expected.line - 1]

    def test_must_cite_names_a_guideline_that_loads(self):
        import asyncio

        from code_review_agent.rag_system import RAGSystem

        rag = RAGSystem()
        asyncio.run(rag._load_guidelines())
        loaded = {g.id for g in rag.guidelines}
        fx = load_fixture(FIXTURE_PATH)
        for ei in fx.expected_issues:
            assert set(ei.must_cite) <= loaded


# ---- offline end-to-end run ---------------------------------------------------


class TestRunnerOffline:
    def test_mock_run_writes_reports(self, tmp_path):
        code = runner_main(
            [
                "--fixture",
                str(FIXTURE_PATH),
                "--mock-llm",
                "--repeats",
                "2",
                "--report",
                str(tmp_path),
            ]
        )
        assert code == 0
        report = json.loads((tmp_path / "py-sql-injection-001.json").read_text())
        meta = report["run_metadata"]
        assert meta["mode"].startswith("mock")
        assert meta["rag_enabled"] is True
        assert "python_best_practices" in meta["corpus_guideline_ids"]
        assert set(meta) >= {"git_commit", "git_uncommitted_changes"}
        run0 = report["files_per_run"][0][0]
        # Both guidelines are retrieved (top_k exceeds the corpus). The
        # language boost itself is tested in test_pipeline.py.
        assert run0["retrieved_guideline_ids"][0] == "python_best_practices"
        assert run0["truncated_inputs"] == []
        assert run0["parse_error"] is None and run0["call_error"] is None
        assert run0["response_model"] == "offline-mock"
        assert report["failed_runs"] == [] and report["reference_run"] == 0
        # The scanner-echo stand-in finds the labeled SQL injection at line 10.
        c0 = report["correctness_per_run"][0]
        assert c0["true_positives"] == 1
        assert c0["negative_assertion_violations"] == []
        assert report["grounding_summary_reference_run"]["citation_rate"] == 0.0
        assert report["consistency"]["runs_scored"] == 2
        assert (tmp_path / "py-sql-injection-001.md").exists()
        assert (tmp_path / "summary.md").exists()

    def test_mock_run_rag_off(self, tmp_path):
        code = runner_main(
            [
                "--fixture",
                str(FIXTURE_PATH),
                "--mock-llm",
                "--no-rag",
                "--report",
                str(tmp_path),
            ]
        )
        assert code == 0
        report = json.loads((tmp_path / "py-sql-injection-001.json").read_text())
        assert report["run_metadata"]["rag_enabled"] is False
        assert report["files_per_run"][0][0]["retrieved_guideline_ids"] == []
        assert report["citation_checks_per_run"][0]["must_cite_retrieved_rate"] == 0.0

    def test_failed_call_is_recorded_and_excluded(self, tmp_path, monkeypatch):
        from code_review_agent.evaluation import mock_llm

        original = mock_llm._Completions.create
        calls = {"n": 0}

        async def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated API error")
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(mock_llm._Completions, "create", flaky)
        code = runner_main(
            [
                "--fixture",
                str(FIXTURE_PATH),
                "--mock-llm",
                "--repeats",
                "3",
                "--report",
                str(tmp_path),
            ]
        )
        assert code == 3
        report = json.loads((tmp_path / "py-sql-injection-001.json").read_text())
        assert report["failed_runs"] == [0] and report["reference_run"] == 1
        assert report["correctness_per_run"][0] is None
        assert report["correctness_across_runs"]["runs"] == 2
        assert report["correctness_across_runs"]["scored_runs"] == [1, 2]
        assert report["files_per_run"][0][0]["call_error"].startswith("RuntimeError")
        assert report["consistency"]["excluded_call_failures"] == 1

    def test_report_file_name_keeps_dots(self, tmp_path):
        data = json.loads(FIXTURE_PATH.read_text())
        data["fixture_id"] = "py-sqli-v1.0"
        path = tmp_path / "fx.json"
        path.write_text(json.dumps(data))
        assert (
            runner_main(["--fixture", str(path), "--mock-llm", "--report", str(tmp_path / "r")])
            == 0
        )
        assert (tmp_path / "r" / "py-sqli-v1.0.json").exists()

    def test_duplicate_and_reserved_fixture_ids_rejected(self, tmp_path):
        from code_review_agent.evaluation.fixtures import load_fixture_directory

        data = json.loads(FIXTURE_PATH.read_text())
        (tmp_path / "a.json").write_text(json.dumps(data))
        (tmp_path / "b.json").write_text(json.dumps(data))
        with pytest.raises(ValueError, match="Duplicate"):
            load_fixture_directory(tmp_path)
        data["fixture_id"] = "summary"
        (tmp_path / "c.json").write_text(json.dumps(data))
        with pytest.raises(ValueError):
            load_fixture(tmp_path / "c.json")

    def test_unsafe_fixture_id_rejected(self, tmp_path):
        data = json.loads(FIXTURE_PATH.read_text())
        data["fixture_id"] = "../escape"
        path = tmp_path / "fx.json"
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            load_fixture(path)

    @pytest.mark.parametrize("flag,value", [("--repeats", "0"), ("--line-tolerance", "-1")])
    def test_invalid_arguments_rejected(self, tmp_path, flag, value):
        with pytest.raises(SystemExit):
            runner_main(
                [
                    "--fixture",
                    str(FIXTURE_PATH),
                    "--mock-llm",
                    flag,
                    value,
                    "--report",
                    str(tmp_path),
                ]
            )

    def test_live_run_without_credentials_exits_cleanly(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_KEY", raising=False)
        monkeypatch.setattr("code_review_agent.evaluation.runner.load_dotenv", lambda *a, **k: None)
        code = runner_main(["--fixture", str(FIXTURE_PATH), "--report", str(tmp_path)])
        assert code == 2
        err = capsys.readouterr().err
        assert "AZURE_OPENAI_KEY" in err and "--mock-llm" in err
