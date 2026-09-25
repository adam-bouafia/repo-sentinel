from datetime import date

from sentinel.agent import RepoResult
from sentinel.report import render_markdown, summary_line, top_findings

RESULTS = [
    RepoResult(repo="o/healthy", status="ok", summary="All green.", cost_usd=0.1),
    RepoResult(
        repo="o/broken",
        status="failing",
        summary="CI red | deps old",
        cost_usd=0.2,
        findings=[
            {
                "severity": "high",
                "category": "ci",
                "title": "Release workflow fails",
                "detail": "Upload step 400s.",
                "evidence": "run 123",
                "pr_url": "https://github.com/o/broken/pull/7",
            },
            {"severity": "low", "category": "docs", "title": "README stale", "detail": "x"},
        ],
    ),
    RepoResult(repo="o/crashed", error="ProcessError: boom"),
]


def test_summary_line_counts_statuses() -> None:
    assert summary_line(RESULTS) == "repo-sentinel: 3 repos - 1 failing, 1 error, 1 ok"


def test_markdown_lists_findings_errors_and_escapes_pipes() -> None:
    md = render_markdown(RESULTS, day=date(2026, 9, 25))
    assert md.startswith("# repo-sentinel report 2026-09-25")
    assert "Cost $0.30" in md
    assert "CI red \\| deps old" in md
    assert "### [high] Release workflow fails" in md
    assert "PR: https://github.com/o/broken/pull/7" in md
    assert "Audit failed: `ProcessError: boom`" in md
    assert "## o/healthy" not in md


def test_top_findings_are_ranked_by_severity() -> None:
    assert top_findings(RESULTS) == [
        "[high] broken: Release workflow fails",
        "[low] broken: README stale",
    ]
