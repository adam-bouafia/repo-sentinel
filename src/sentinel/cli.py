"""Command line entry point: `sentinel run`."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
from datetime import date
from pathlib import Path

from sentinel import notify
from sentinel.agent import audit_all
from sentinel.config import load_config
from sentinel.report import render_markdown, summary_line, top_findings
from sentinel.state import load_state, previous_findings, save_state, update_state


def _notify_failed(channel: str, error: Exception) -> None:
    """Log a failed channel; in GitHub Actions also as a warning on the run page.

    Why: a broken channel must not fail the run, but a line buried in the log is
    easy to miss.
    """
    message = f"notify {channel} failed: {error}"
    print(message, file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=notify {channel}::{message}", flush=True)


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    repos = cfg.repos
    if args.repo:
        wanted = set(args.repo)
        repos = [r for r in repos if r.name in wanted or r.name.split("/")[-1] in wanted]
        if not repos:
            print(f"no configured repo matches {args.repo}", file=sys.stderr)
            return 2
    agent_cfg = dataclasses.replace(cfg.agent, allow_prs=cfg.agent.allow_prs and not args.no_pr)

    print(
        f"auditing {len(repos)} repo(s) with {agent_cfg.model} "
        f"(PRs {'on' if agent_cfg.allow_prs else 'off'})",
        flush=True,
    )
    today = date.today()
    state_path = cfg.report_dir / "state.json"
    state = load_state(state_path)
    results = await audit_all(
        repos,
        agent_cfg,
        previous={r.name: previous_findings(state, r.name) for r in repos},
        log_dir=cfg.report_dir / "runs" / today.isoformat(),
        verbose=args.verbose,
    )
    for result in results:
        update_state(state, result, today)
    save_state(state_path, state)

    markdown = render_markdown(results, day=today)
    report_path = cfg.report_dir / f"{today.isoformat()}.md"
    report_path.write_text(markdown)
    headline = summary_line(results)
    print(f"{headline}\nreport: {report_path}")

    if args.no_notify:
        return 0
    n = cfg.notify
    top = "\n".join(top_findings(results)) or "Nothing to report."
    channels = {
        "desktop": lambda: notify.desktop(headline, top, report_path),
        "telegram": lambda: notify.telegram(f"{headline}\n\n{top}"),
        "email": lambda: notify.email(n["email"], headline, markdown),
    }
    for name, send in channels.items():
        if n.get(name, {}).get("enabled"):
            try:
                send()
            except Exception as e:  # noqa: BLE001 - a broken channel must not hide the others
                _notify_failed(name, e)
    if n.get("github_issue", {}).get("enabled"):
        try:
            print("digest:", await notify.github_issue(n["github_issue"]["repo"], markdown))
        except Exception as e:  # noqa: BLE001
            _notify_failed("github_issue", e)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="sentinel", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="audit the configured repos")
    p.add_argument("-c", "--config", type=Path, default=Path("sentinel.toml"))
    p.add_argument("-r", "--repo", action="append", help="only this repo (owner/name or name)")
    p.add_argument("--no-pr", action="store_true", help="report only, never open PRs")
    p.add_argument("--no-notify", action="store_true", help="write the report file only")
    p.add_argument("-v", "--verbose", action="store_true", help="print tool calls as they happen")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))
