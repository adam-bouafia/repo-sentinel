from datetime import date

from sentinel.agent import RepoResult
from sentinel.report import render_html, render_markdown, summary_line, top_findings

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
                "seen": "new",
            },
            {
                "severity": "low",
                "category": "docs",
                "title": "README stale",
                "detail": "x",
                "seen": "recurring",
                "first_seen": "2026-09-01",
            },
        ],
        resolved=[{"key": "k", "severity": "medium", "title": "Old action pinned"}],
    ),
    RepoResult(repo="o/crashed", error="ProcessError: boom"),
]


def test_summary_line_counts_statuses() -> None:
    assert summary_line(RESULTS) == "repo-sentinel: 3 repos - 1 failing, 1 error, 1 ok"


def test_summary_line_uses_singular_for_one_repo() -> None:
    assert summary_line(RESULTS[:1]) == "repo-sentinel: 1 repo - 1 ok"


def test_markdown_lists_findings_errors_and_escapes_pipes() -> None:
    md = render_markdown(RESULTS, day=date(2026, 9, 25))
    assert md.startswith("# repo-sentinel report 2026-09-25")
    assert "Cost $0.30" in md
    assert "CI red \\| deps old" in md
    assert "### [high] Release workflow fails" in md
    assert "PR: https://github.com/o/broken/pull/7" in md
    assert "Audit failed: `ProcessError: boom`" in md
    assert "## o/healthy" not in md


def test_markdown_marks_new_recurring_and_resolved() -> None:
    md = render_markdown(RESULTS, day=date(2026, 9, 25))
    assert "| o/broken | FAIL | 2 (1 new) |" in md
    assert "*ci* - new" in md
    assert "*docs* - open since 2026-09-01" in md
    assert "### Resolved since last run\n\n- [medium] Old action pinned" in md


def test_top_findings_are_ranked_by_severity() -> None:
    assert top_findings(RESULTS) == [
        "[high] broken: Release workflow fails (new)",
        "[low] broken: README stale",
    ]


def test_html_email_escapes_text_and_shows_severity_and_resolved() -> None:
    results = [
        RepoResult(
            repo="o/r",
            status="warning",
            summary="Uses <script> in `ci.yml`",
            findings=[{**RESULTS[1].findings[0], "detail": "a < b"}],
            resolved=RESULTS[1].resolved,
        )
    ]
    page = render_html(results, day=date(2026, 9, 25))
    assert "&lt;script&gt;" in page and "<script>" not in page
    assert "<code" in page and "ci.yml</code>" in page
    assert "a &lt; b" in page
    assert ">high</span>" in page
    assert "Resolved since last run" in page and "Old action pinned" in page
