"""Render audit results as Markdown and a one-line summary."""

from __future__ import annotations

from collections import Counter
from datetime import date

from sentinel.agent import SEVERITIES, RepoResult

STATUS_ICON = {"ok": "OK", "warning": "WARN", "failing": "FAIL", "error": "ERROR"}


def summary_line(results: list[RepoResult]) -> str:
    counts = Counter(r.status for r in results)
    parts = [f"{counts[s]} {s}" for s in ("failing", "warning", "error", "ok") if counts[s]]
    return f"repo-sentinel: {len(results)} repos - " + ", ".join(parts)


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
        lines.append(f"| {r.repo} | {STATUS_ICON[r.status]} | {len(r.findings)} | {summary} |")

    for r in results:
        if not r.findings and not r.error:
            continue
        lines += ["", f"## {r.repo}", ""]
        if r.error:
            lines += [f"Audit failed: `{r.error}`", ""]
        for f in r.findings:
            lines.append(f"### [{f['severity']}] {f['title']}")
            lines.append(f"*{f['category']}*")
            lines += ["", f["detail"]]
            if f.get("evidence"):
                lines += ["", f"Evidence: {f['evidence']}"]
            if f.get("suggested_fix"):
                lines += ["", f"Fix: {f['suggested_fix']}"]
            if f.get("pr_url"):
                lines += ["", f"PR: {f['pr_url']}"]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def top_findings(results: list[RepoResult], limit: int = 5) -> list[str]:
    ranked = sorted(
        ((f, r.repo) for r in results for f in r.findings),
        key=lambda x: SEVERITIES.index(x[0]["severity"]),
    )
    return [f"[{f['severity']}] {repo.split('/')[-1]}: {f['title']}" for f, repo in ranked[:limit]]
