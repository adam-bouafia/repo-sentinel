from datetime import date

from sentinel.agent import RepoResult
from sentinel.state import load_state, previous_findings, save_state, update_state


def finding(key: str, severity: str = "low") -> dict[str, str]:
    return {"key": key, "severity": severity, "category": "ci", "title": key, "detail": "x"}


def test_first_run_marks_everything_new() -> None:
    state: dict = {}
    result = RepoResult(repo="o/r", status="warning", findings=[finding("a")])
    update_state(state, result, date(2026, 9, 1))
    assert result.findings[0]["seen"] == "new"
    assert state["o/r"]["a"]["first_seen"] == "2026-09-01"
    assert result.resolved == []


def test_second_run_splits_recurring_new_and_resolved() -> None:
    state: dict = {}
    update_state(
        state, RepoResult(repo="o/r", findings=[finding("a"), finding("b")]), date(2026, 9, 1)
    )
    result = RepoResult(repo="o/r", findings=[finding("a"), finding("c")])
    update_state(state, result, date(2026, 9, 8))
    seen = {f["key"]: (f["seen"], f["first_seen"]) for f in result.findings}
    assert seen == {"a": ("recurring", "2026-09-01"), "c": ("new", "2026-09-08")}
    assert [f["key"] for f in result.resolved] == ["b"]
    assert set(state["o/r"]) == {"a", "c"}


def test_failed_audit_keeps_previous_state() -> None:
    state: dict = {}
    update_state(state, RepoResult(repo="o/r", findings=[finding("a")]), date(2026, 9, 1))
    failed = RepoResult(repo="o/r", error="boom")
    update_state(state, failed, date(2026, 9, 8))
    assert set(state["o/r"]) == {"a"}
    assert failed.resolved == []


def test_state_round_trips_through_disk(tmp_path) -> None:
    path = tmp_path / "sub" / "state.json"
    assert load_state(path) == {}
    state: dict = {}
    update_state(state, RepoResult(repo="o/r", findings=[finding("a", "high")]), date(2026, 9, 1))
    save_state(path, state)
    assert previous_findings(load_state(path), "o/r") == [
        {"key": "a", "title": "a", "severity": "high", "category": "ci", "first_seen": "2026-09-01"}
    ]
