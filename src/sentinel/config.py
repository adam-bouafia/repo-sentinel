"""Load and validate sentinel.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RepoConfig:
    name: str
    notes: str = ""
    # finding key -> reason; the agent does not report these and the code drops them
    accepted: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentConfig:
    model: str = "claude-opus-5"
    effort: str = "high"
    max_turns: int = 40
    max_budget_usd: float = 3.0
    task_budget_tokens: int = 150_000
    concurrency: int = 2
    allow_prs: bool = True
    review_prs: bool = True  # an independent reviewer must approve every fix PR
    review_model: str = ""  # empty = same as model
    review_budget_usd: float = 0.5


@dataclass(frozen=True)
class Config:
    repos: list[RepoConfig]
    agent: AgentConfig = field(default_factory=AgentConfig)
    report_dir: Path = Path("reports")
    notify: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_config(path: Path) -> Config:
    """Parse the TOML config.

    Raises:
        ValueError: if no repos are configured or a repo name is not owner/name.
    """
    raw = tomllib.loads(path.read_text())
    repos = [
        RepoConfig(name=r["name"], notes=r.get("notes", "").strip(), accepted=r.get("accepted", {}))
        for r in raw.get("repos", [])
    ]
    if not repos:
        raise ValueError(f"{path}: no [[repos]] configured")
    for repo in repos:
        if repo.name.count("/") != 1:
            raise ValueError(f"{path}: repo name must be owner/name, got {repo.name!r}")

    agent = AgentConfig(**raw.get("agent", {}))
    report_dir = Path(raw.get("report", {}).get("dir", "reports"))
    if not report_dir.is_absolute():
        report_dir = path.parent / report_dir
    return Config(repos=repos, agent=agent, report_dir=report_dir, notify=raw.get("notify", {}))
