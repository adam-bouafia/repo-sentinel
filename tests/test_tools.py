import base64
from typing import Any

import pytest

from sentinel import tools
from sentinel.agent import FINDINGS_SCHEMA, _prompt, build_options
from sentinel.config import AgentConfig, RepoConfig

PR = {"title": "Bump x", "body": "b", "files": [{"path": "a", "content": "x==2.0\n"}]}


def open_fix_pr(allow_prs: bool = True, reviewer: Any = None) -> Any:
    by_name = {
        t.name: t for t in tools.build_repo_tools("o/r", allow_prs=allow_prs, reviewer=reviewer)
    }
    return by_name["open_fix_pr"].handler


def fake_github(old_content: str = "x==1.0\n") -> tuple[Any, list[str]]:
    """GitHub API stand-in that records calls and serves one existing file."""
    calls: list[str] = []

    async def fake_api(path: str, *, method: str = "GET", **_k: Any) -> Any:
        calls.append(f"{method} {path}")
        if path.startswith("repos/o/r/pulls") and method == "GET":
            return []
        if path == "repos/o/r":
            return {"default_branch": "main"}
        if path.startswith("repos/o/r/contents/"):
            return {"content": base64.b64encode(old_content.encode()).decode()}
        return {"object": {"sha": "s"}, "tree": {"sha": "t"}, "sha": "c", "html_url": "u"}

    return fake_api, calls


async def test_open_fix_pr_refuses_when_prs_disabled() -> None:
    result = await open_fix_pr(allow_prs=False)(
        {"title": "t", "body": "b", "files": [{"path": "a", "content": "x"}]}
    )
    assert result["is_error"] and "disabled" in result["content"][0]["text"]


@pytest.mark.parametrize(
    "files",
    [
        [],
        [{"path": f"f{i}", "content": "x"} for i in range(tools.MAX_PR_FILES + 1)],
        [{"path": "/etc/passwd", "content": "x"}],
        [{"path": "../outside", "content": "x"}],
    ],
)
async def test_open_fix_pr_rejects_bad_file_sets_before_calling_github(
    monkeypatch: pytest.MonkeyPatch, files: list[dict[str, str]]
) -> None:
    async def no_network(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("GitHub must not be called")

    monkeypatch.setattr(tools, "api", no_network)
    result = await open_fix_pr()({"title": "t", "body": "b", "files": files})
    assert result["is_error"]


async def test_open_fix_pr_skips_duplicate_sentinel_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_api(path: str, **_k: Any) -> Any:
        assert path.startswith("repos/o/r/pulls"), "must stop after the duplicate check"
        return [
            {
                "title": "Bump x",
                "head": {"ref": "sentinel/bump-x-20260101"},
                "html_url": "https://github.com/o/r/pull/1",
            }
        ]

    monkeypatch.setattr(tools, "api", fake_api)
    result = await open_fix_pr()(
        {"title": "Bump x", "body": "b", "files": [{"path": "a", "content": "x"}]}
    )
    assert result["is_error"] and "pull/1" in result["content"][0]["text"]


def test_agent_has_no_shell_or_file_write_tools(tmp_path: Any) -> None:
    opts = build_options(RepoConfig("o/r"), AgentConfig(), str(tmp_path))
    assert opts.tools == ["WebFetch", "WebSearch"]
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert not {"Bash", "Write", "Edit"} & set(opts.allowed_tools)


async def test_open_fix_pr_rejects_bump_missing_from_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "api", fake_github()[0])
    bumps = [{"source": "pypi", "name": "x", "version": "3.0"}]
    result = await open_fix_pr()({**PR, "bumps": bumps})
    assert result["is_error"] and "does not appear" in result["content"][0]["text"]


async def test_open_fix_pr_refuses_unpublished_version(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api, calls = fake_github()
    monkeypatch.setattr(tools, "api", fake_api)

    async def missing(*_a: Any) -> bool:
        return False

    monkeypatch.setattr(tools, "version_exists", missing)
    bumps = [{"source": "pypi", "name": "x", "version": "2.0"}]
    result = await open_fix_pr()({**PR, "bumps": bumps})
    assert result["is_error"] and "has no x 2.0" in result["content"][0]["text"]
    assert not any(c.startswith("POST") for c in calls)


async def test_open_fix_pr_refuses_file_longer_than_read_file_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_api, calls = fake_github("y" * tools.MAX_FILE_CHARS)
    monkeypatch.setattr(tools, "api", fake_api)
    result = await open_fix_pr()(PR)
    assert result["is_error"] and "longer than" in result["content"][0]["text"]
    assert not any(c.startswith("POST") for c in calls)


async def test_reviewer_sees_diff_and_rejection_blocks_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api, calls = fake_github()
    monkeypatch.setattr(tools, "api", fake_api)
    seen: list[str] = []

    async def reviewer(text: str) -> tuple[bool, str]:
        seen.append(text)
        return False, "major bump breaks the API"

    result = await open_fix_pr(reviewer=reviewer)(PR)
    assert result["is_error"] and "major bump breaks the API" in result["content"][0]["text"]
    assert "-x==1.0" in seen[0] and "+x==2.0" in seen[0]
    assert not any(c.startswith("POST") for c in calls)


async def test_approved_pr_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api, calls = fake_github()
    monkeypatch.setattr(tools, "api", fake_api)

    async def reviewer(_text: str) -> tuple[bool, str]:
        return True, "ok"

    result = await open_fix_pr(reviewer=reviewer)(PR)
    assert not result.get("is_error")
    assert "POST repos/o/r/pulls" in calls


def test_every_finding_needs_a_key_and_evidence() -> None:
    required = FINDINGS_SCHEMA["properties"]["findings"]["items"]["required"]
    assert {"key", "evidence"} <= set(required)


def test_prompt_carries_accepted_and_previous_findings() -> None:
    repo = RepoConfig("o/r", accepted={"shexli-51": "known false positive"})
    prev = [{"key": "old-action", "severity": "medium", "title": "checkout@v4"}]
    prompt = _prompt(repo, allow_prs=True, previous=prev)
    assert "- shexli-51: known false positive" in prompt
    assert "- old-action [medium] checkout@v4" in prompt
