from pathlib import Path

import pytest

from sentinel.config import load_config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sentinel.toml"
    path.write_text(text)
    return path


def test_loads_repos_agent_and_resolves_report_dir(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
[agent]
model = "claude-opus-5"
max_budget_usd = 0.5

[[repos]]
name = "owner/one"
notes = "  context  "
""",
        )
    )
    assert [r.name for r in cfg.repos] == ["owner/one"]
    assert cfg.repos[0].notes == "context"
    assert cfg.agent.max_budget_usd == 0.5
    assert cfg.agent.allow_prs is True
    assert cfg.report_dir == tmp_path / "reports"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[agent]\nmodel = 'x'\n", "no \\[\\[repos\\]\\]"),
        ("[[repos]]\nname = 'no-owner'\n", "owner/name"),
    ],
)
def test_rejects_invalid_config(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(write(tmp_path, text))


def test_repo_config_file_parses() -> None:
    cfg = load_config(Path(__file__).parent.parent / "sentinel.toml")
    assert cfg.repos
