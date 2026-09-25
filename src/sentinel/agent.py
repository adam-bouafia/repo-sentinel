"""Run one audit agent per repo with the Claude Agent SDK."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

from sentinel.config import AgentConfig, RepoConfig
from sentinel.tools import SERVER_NAME, TOOL_NAMES, build_repo_server

SEVERITIES = ["critical", "high", "medium", "low", "info"]

FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "warning", "failing"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "category": {
                        "type": "string",
                        "enum": [
                            "ci",
                            "security",
                            "dependency",
                            "upstream",
                            "release",
                            "metadata",
                            "docs",
                            "other",
                        ],
                    },
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "evidence": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                    "pr_url": {"type": "string"},
                },
                "required": ["severity", "category", "title", "detail"],
            },
        },
    },
    "required": ["status", "summary", "findings"],
}

SYSTEM_PROMPT = """\
You are repo-sentinel, an auditor for a single GitHub repository owned by a
DevOps engineer. Your job is to find what is broken, outdated or drifting, and
report it precisely.

Work through the repository with your tools:
- Start with repo_overview. For each workflow, look at its most recent run on the
  default branch or a release tag. If that run failed, use failed_run_log to find
  the root cause. Ignore older failures that a later run of the same workflow has
  already fixed.
- Read the manifests that matter for this project (pyproject.toml, package.json,
  metadata.json, requirements*.txt, Dockerfiles, workflow files) and compare pinned
  versions with current upstream releases. Use upstream_version for PyPI, npm and
  GitHub projects (including GitHub Actions). Use WebFetch or WebSearch only for
  what it cannot answer, such as platform release announcements. Do not guess
  version numbers.
- Check GitHub Actions for deprecated action majors and runner images.
- Check the project-specific points in the maintainer notes.

Findings must be concrete: name the file, version, run or URL as evidence.
Never guess what a coded value means (numeric statuses, enum fields). Use the
definition from the maintainer notes or official documentation; if you cannot
find one, report the raw value and say its meaning is unconfirmed.
Leave out anything that is fine. Severity: critical = broken for users or a
security issue; high = CI failing or a release blocked; medium = outdated in a
way that will break soon; low = housekeeping; info = worth knowing.

You may call open_fix_pr for small, mechanical, low-risk fixes you are certain
about (a version bump, a metadata field). Provide complete file contents. One
PR per logical fix. Put the PR URL on the matching finding. Never open a PR for
anything that needs judgement or testing; report it instead. If a sentinel PR for
the same fix is already open, do not open another.

Finish with the structured report. status is failing if anything is critical or
high, warning if anything is medium, otherwise ok.
"""


@dataclass
class RepoResult:
    repo: str
    status: str = "error"
    summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    turns: int = 0
    error: str | None = None


def _prompt(repo: RepoConfig, *, allow_prs: bool) -> str:
    notes = f"\n\nMaintainer notes:\n{repo.notes}" if repo.notes else ""
    prs = "" if allow_prs else " PRs are disabled for this run: do not call open_fix_pr."
    return f"Audit {repo.name}. Today is {date.today().isoformat()}.{prs}{notes}"


def build_options(repo: RepoConfig, cfg: AgentConfig, workdir: str) -> ClaudeAgentOptions:
    """Agent options for one repo.

    Why these settings: no Bash/Write/Edit (tools= restricts built-ins to web lookups),
    dontAsk denies anything not allow-listed, setting_sources=[] keeps the host's
    CLAUDE.md, skills and settings out of the run, and cwd is an empty temp dir.
    """
    return ClaudeAgentOptions(
        model=cfg.model,
        effort=cfg.effort,
        system_prompt=SYSTEM_PROMPT,
        tools=["WebFetch", "WebSearch"],
        allowed_tools=[*TOOL_NAMES, "WebFetch", "WebSearch"],
        mcp_servers={SERVER_NAME: build_repo_server(repo.name, allow_prs=cfg.allow_prs)},
        permission_mode="dontAsk",
        setting_sources=[],
        cwd=workdir,
        max_turns=cfg.max_turns,
        max_budget_usd=cfg.max_budget_usd,
        task_budget={"total": cfg.task_budget_tokens},
        output_format={"type": "json_schema", "schema": FINDINGS_SCHEMA},
    )


async def audit_repo(repo: RepoConfig, cfg: AgentConfig, *, verbose: bool = False) -> RepoResult:
    """Run the agent on one repo. Never raises; failures come back as status=error."""
    result = RepoResult(repo=repo.name)
    try:
        with tempfile.TemporaryDirectory(prefix="sentinel-") as workdir:
            async for message in query(
                prompt=_prompt(repo, allow_prs=cfg.allow_prs),
                options=build_options(repo, cfg, workdir),
            ):
                if verbose and isinstance(message, AssistantMessage):
                    for block in message.content:
                        name = getattr(block, "name", None)
                        if name:
                            print(f"  [{repo.name}] -> {name}", flush=True)
                if isinstance(message, ResultMessage):
                    result.cost_usd = message.total_cost_usd or 0.0
                    result.turns = message.num_turns
                    out = message.structured_output
                    if message.is_error or not isinstance(out, dict):
                        result.error = f"{message.subtype}: {message.result or message.errors}"
                    else:
                        result.status = out["status"]
                        result.summary = out["summary"]
                        result.findings = sorted(
                            out["findings"], key=lambda f: SEVERITIES.index(f["severity"])
                        )
    except Exception as e:  # noqa: BLE001 - one bad repo must not abort the run
        result.error = f"{type(e).__name__}: {e}"
    return result


async def audit_all(
    repos: list[RepoConfig], cfg: AgentConfig, *, verbose: bool = False
) -> list[RepoResult]:
    sem = asyncio.Semaphore(cfg.concurrency)

    async def one(repo: RepoConfig) -> RepoResult:
        async with sem:
            return await audit_repo(repo, cfg, verbose=verbose)

    return await asyncio.gather(*(one(r) for r in repos))
