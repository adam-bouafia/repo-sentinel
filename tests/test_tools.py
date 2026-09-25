from typing import Any

import pytest

from sentinel import tools
from sentinel.agent import build_options
from sentinel.config import AgentConfig, RepoConfig


def open_fix_pr(allow_prs: bool = True) -> Any:
    by_name = {t.name: t for t in tools.build_repo_tools("o/r", allow_prs=allow_prs)}
    return by_name["open_fix_pr"].handler


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
