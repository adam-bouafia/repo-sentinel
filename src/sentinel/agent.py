"""Run one audit agent per repo with the Claude Agent SDK."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from sentinel.config import AgentConfig, RepoConfig
from sentinel.tools import SERVER_NAME, TOOL_NAMES, Reviewer, build_repo_server

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
                    "key": {
                        "type": "string",
                        "description": "stable kebab-case id for this issue, e.g. "
                        "actions-checkout-outdated; reuse the previous run's key for the "
                        "same issue",
                    },
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
                "required": ["key", "severity", "category", "title", "detail", "evidence"],
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

Findings must be concrete: every finding needs evidence naming the file, version,
run or URL. A claim you cannot back with evidence is not a finding.
Never guess what a coded value means (numeric statuses, enum fields). Use the
definition from the maintainer notes or official documentation; if you cannot
find one, report the raw value and say its meaning is unconfirmed.
Leave out anything that is fine. Severity: critical = broken for users or a
security issue; high = CI failing or a release blocked; medium = outdated in a
way that will break soon; low = housekeeping; info = worth knowing.

You may call open_fix_pr for small, mechanical, low-risk fixes you are certain
about (a version bump, a metadata field). Provide complete file contents and list
every version bump in bumps. One PR per logical fix. If open_fix_pr refuses or its
reviewer rejects the PR, do not retry it; report the fix as a finding. Put the PR
URL on the matching finding. Never open a PR for anything that needs judgement or
testing; report it instead. If a sentinel PR for the same fix is already open, do
not open another.

Each finding has a key. If the prompt lists previous findings, reuse the same key
for the same issue, even if its details changed. Do not report anything the
maintainer has accepted.

Finish with the structured report. status is failing if anything is critical or
high, warning if anything is medium, otherwise ok.
"""


@dataclass
class RepoResult:
    repo: str
    status: str = "error"
    summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    resolved: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    turns: int = 0
    error: str | None = None


def _prompt(
    repo: RepoConfig, *, allow_prs: bool, previous: list[dict[str, Any]] | None = None
) -> str:
    parts = [f"Audit {repo.name}. Today is {date.today().isoformat()}."]
    if not allow_prs:
        parts.append("PRs are disabled for this run: do not call open_fix_pr.")
    if repo.notes:
        parts.append(f"Maintainer notes:\n{repo.notes}")
    if repo.accepted:
        lines = "\n".join(f"- {k}: {why}" for k, why in repo.accepted.items())
        parts.append(f"Accepted by the maintainer, do not report:\n{lines}")
    if previous:
        lines = "\n".join(f"- {f['key']} [{f['severity']}] {f['title']}" for f in previous)
        parts.append(
            "Findings from the previous run. Check whether each still holds, and reuse "
            f"its key if it does:\n{lines}"
        )
    return "\n\n".join(parts)


REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"approve": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["approve", "reason"],
}

REVIEW_PROMPT = """\
You review pull requests that an automated auditor wants to open on a repository.
You did not write the change and you do not see the auditor's reasoning, only the
title, body and diff. Approve only if all of these hold:
- the diff does exactly what the title and body say, and nothing else
- it is mechanical and low-risk: a version bump, a metadata field, a typo
- no file is truncated or has unrelated lines removed
- it cannot break the build or runtime without a code change elsewhere; for a
  major version bump, check the upstream release notes with WebFetch
Otherwise reject, and say why in one or two sentences.
"""


async def review_pr(pr_text: str, cfg: AgentConfig) -> tuple[bool, str, float]:
    """Independent second opinion on a fix PR.

    Why: the auditor that wrote a fix is the worst judge of it. The reviewer gets
    a fresh session with only the diff and read-only web access.

    Returns:
        (approved, reason, cost_usd). Any failure counts as a rejection.
    """
    with tempfile.TemporaryDirectory(prefix="sentinel-review-") as workdir:
        options = ClaudeAgentOptions(
            model=cfg.review_model or cfg.model,
            system_prompt=REVIEW_PROMPT,
            tools=["WebFetch"],
            allowed_tools=["WebFetch"],
            permission_mode="dontAsk",
            setting_sources=[],
            cwd=workdir,
            max_turns=8,
            max_budget_usd=cfg.review_budget_usd,
            output_format={"type": "json_schema", "schema": REVIEW_SCHEMA},
        )
        final: ResultMessage | None = None
        try:
            async for message in query(prompt=pr_text, options=options):
                if isinstance(message, ResultMessage):
                    final = message
        except Exception as e:  # noqa: BLE001 - no review means no PR
            return False, f"review failed: {type(e).__name__}: {e}", 0.0
    if final is None:
        return False, "review returned no result", 0.0
    cost = final.total_cost_usd or 0.0
    out = final.structured_output
    if final.is_error or not isinstance(out, dict):
        return False, f"review failed: {final.subtype}", cost
    return bool(out["approve"]), out["reason"], cost


def build_options(
    repo: RepoConfig,
    cfg: AgentConfig,
    workdir: str,
    *,
    reviewer: Reviewer | None = None,
) -> ClaudeAgentOptions:
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
        mcp_servers={
            SERVER_NAME: build_repo_server(repo.name, allow_prs=cfg.allow_prs, reviewer=reviewer)
        },
        permission_mode="dontAsk",
        setting_sources=[],
        cwd=workdir,
        max_turns=cfg.max_turns,
        max_budget_usd=cfg.max_budget_usd,
        task_budget={"total": cfg.task_budget_tokens},
        output_format={"type": "json_schema", "schema": FINDINGS_SCHEMA},
    )


LOG_CHARS = 500


def _log_event(log: list[dict[str, Any]], message: Any) -> None:
    """Append tool calls and tool results to the run log."""
    if not isinstance(message, AssistantMessage | UserMessage) or isinstance(message.content, str):
        return
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for block in message.content:
        if isinstance(block, ToolUseBlock):
            args = json.dumps(block.input, default=str)[:LOG_CHARS]
            log.append({"at": now, "tool": block.name, "input": args})
        elif isinstance(block, ToolResultBlock):
            content = block.content if isinstance(block.content, str) else json.dumps(block.content)
            log.append({"at": now, "result": content[:LOG_CHARS], "error": bool(block.is_error)})


async def audit_repo(
    repo: RepoConfig,
    cfg: AgentConfig,
    *,
    previous: list[dict[str, Any]] | None = None,
    log_dir: Path | None = None,
    verbose: bool = False,
) -> RepoResult:
    """Run the agent on one repo. Never raises; failures come back as status=error.

    Args:
        repo: the repo and its maintainer notes and accepted findings.
        cfg: agent settings.
        previous: last run's findings for this repo, so keys stay stable.
        log_dir: where to write the JSONL log of tool calls; None disables it.
        verbose: print tool calls as they happen.
    """
    result = RepoResult(repo=repo.name)
    review_cost = 0.0
    log: list[dict[str, Any]] = []

    async def reviewer(pr_text: str) -> tuple[bool, str]:
        nonlocal review_cost
        approved, reason, cost = await review_pr(pr_text, cfg)
        review_cost += cost
        log.append({"review": "approved" if approved else "rejected", "reason": reason})
        return approved, reason

    try:
        with tempfile.TemporaryDirectory(prefix="sentinel-") as workdir:
            async for message in query(
                prompt=_prompt(repo, allow_prs=cfg.allow_prs, previous=previous),
                options=build_options(
                    repo, cfg, workdir, reviewer=reviewer if cfg.review_prs else None
                ),
            ):
                _log_event(log, message)
                if verbose and isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            print(f"  [{repo.name}] -> {block.name}", flush=True)
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
                            (f for f in out["findings"] if f["key"] not in repo.accepted),
                            key=lambda f: SEVERITIES.index(f["severity"]),
                        )
    except Exception as e:  # noqa: BLE001 - one bad repo must not abort the run
        result.error = f"{type(e).__name__}: {e}"
    result.cost_usd += review_cost
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{repo.name.replace('/', '__')}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in log))
    return result


async def audit_all(
    repos: list[RepoConfig],
    cfg: AgentConfig,
    *,
    previous: dict[str, list[dict[str, Any]]] | None = None,
    log_dir: Path | None = None,
    verbose: bool = False,
) -> list[RepoResult]:
    sem = asyncio.Semaphore(cfg.concurrency)
    previous = previous or {}

    async def one(repo: RepoConfig) -> RepoResult:
        async with sem:
            return await audit_repo(
                repo, cfg, previous=previous.get(repo.name), log_dir=log_dir, verbose=verbose
            )

    return await asyncio.gather(*(one(r) for r in repos))
