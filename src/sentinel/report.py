"""Render audit results as Markdown and a one-line summary."""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from sentinel.agent import SEVERITIES, RepoResult

STATUS_ICON = {"ok": "OK", "warning": "WARN", "failing": "FAIL", "error": "ERROR"}


def summary_line(results: list[RepoResult]) -> str:
    counts = Counter(r.status for r in results)
    parts = [f"{counts[s]} {s}" for s in ("failing", "warning", "error", "ok") if counts[s]]
    noun = "repo" if len(results) == 1 else "repos"
    return f"repo-sentinel: {len(results)} {noun} - " + ", ".join(parts)


def render_markdown(results: list[RepoResult], *, day: date | None = None) -> str:
    day = day or date.today()
    total_cost = sum(r.cost_usd for r in results)
    lines = [
        f"# repo-sentinel report {day.isoformat()}",
        "",
        summary_line(results) + f". Cost ${total_cost:.2f}.",
        "",
        "| Repo | Status | Findings | Summary |",
        "|---|---|---|---|",
    ]
    for r in results:
        summary = (r.error or r.summary).replace("|", "\\|").replace("\n", " ")
        new = sum(f.get("seen") == "new" for f in r.findings)
        count = f"{len(r.findings)} ({new} new)" if new else str(len(r.findings))
        lines.append(f"| {r.repo} | {STATUS_ICON[r.status]} | {count} | {summary} |")

    for r in results:
        if not r.findings and not r.error and not r.resolved:
            continue
        lines += ["", f"## {r.repo}", ""]
        if r.error:
            lines += [f"Audit failed: `{r.error}`", ""]
        for f in r.findings:
            lines.append(f"### [{f['severity']}] {f['title']}")
            lines.append(f"*{f['category']}* - {_seen(f)}")
            lines += ["", f["detail"]]
            if f.get("evidence"):
                lines += ["", f"Evidence: {f['evidence']}"]
            if f.get("suggested_fix"):
                lines += ["", f"Fix: {f['suggested_fix']}"]
            if f.get("pr_url"):
                lines += ["", f"PR: {f['pr_url']}"]
            lines.append("")
        if r.resolved:
            lines += ["### Resolved since last run", ""]
            lines += [f"- [{f['severity']}] {f['title']}" for f in r.resolved]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _seen(finding: dict[str, Any]) -> str:
    if finding.get("seen") == "recurring":
        return f"open since {finding['first_seen']}"
    return "new"


def top_findings(results: list[RepoResult], limit: int = 5) -> list[str]:
    ranked = sorted(
        ((f, r.repo) for r in results for f in r.findings),
        key=lambda x: SEVERITIES.index(x[0]["severity"]),
    )
    return [
        f"[{f['severity']}] {repo.split('/')[-1]}: {f['title']}"
        + (" (new)" if f.get("seen") == "new" else "")
        for f, repo in ranked[:limit]
    ]
