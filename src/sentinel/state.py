"""Findings memory between runs: what is new, still open, or resolved since last time.

Why: without it every audit starts from zero, the report cannot say what
changed, and known non-issues have to be restated in the notes forever.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from sentinel.agent import RepoResult

State = dict[str, dict[str, dict[str, Any]]]


def load_state(path: Path) -> State:
    """Read the state file, or an empty state if there is none yet."""
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def previous_findings(state: State, repo: str) -> list[dict[str, Any]]:
    """Last run's findings for `repo`, as the agent sees them in its prompt."""
    return [{"key": k, **v} for k, v in state.get(repo, {}).items()]


def update_state(state: State, result: RepoResult, day: date) -> None:
    """Tag each finding new or recurring, record what was resolved, and store the run.

    A failed audit leaves the repo's state untouched, so an error never reads as
    "everything resolved".
    """
    if result.error:
        return
    prev = state.get(result.repo, {})
    current: dict[str, dict[str, Any]] = {}
    for f in result.findings:
        old = prev.get(f["key"])
        f["first_seen"] = old["first_seen"] if old else day.isoformat()
        f["seen"] = "recurring" if old else "new"
        current[f["key"]] = {
            "title": f["title"],
            "severity": f["severity"],
            "category": f["category"],
            "first_seen": f["first_seen"],
        }
    result.resolved = [{"key": k, **v} for k, v in prev.items() if k not in current]
    state[result.repo] = current
