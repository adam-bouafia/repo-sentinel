"""Render audit results as Markdown and a one-line summary."""

from __future__ import annotations

import html
import re
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


SEVERITY_COLOR = {
    "critical": "#b42318",
    "high": "#c4320a",
    "medium": "#b54708",
    "low": "#3c6eb4",
    "info": "#667085",
}
STATUS_COLOR = {"ok": "#067647", "warning": "#b54708", "failing": "#b42318", "error": "#b42318"}
FONT = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"


def _inline(text: str) -> str:
    """Escape text for HTML, keep `code` spans and line breaks."""
    escaped = html.escape(text)
    escaped = re.sub(
        r"`([^`]+)`",
        r'<code style="background:#f2f4f7;padding:1px 4px;border-radius:4px;'
        r'font-size:13px">\1</code>',
        escaped,
    )
    return escaped.replace("\n", "<br>")


def _pill(label: str, color: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f"background:{color};color:#fff;font-size:12px;font-weight:600;"
        f'text-transform:uppercase;letter-spacing:.04em">{html.escape(label)}</span>'
    )


def _finding_html(f: dict[str, Any]) -> str:
    rows = [
        f'<div style="margin:0 0 6px">{_pill(f["severity"], SEVERITY_COLOR[f["severity"]])}'
        f'<span style="color:#667085;font-size:13px;margin-left:8px">'
        f"{html.escape(f['category'])} &middot; {_seen(f)}</span></div>",
        f'<div style="font-size:16px;font-weight:600;margin:0 0 8px">{_inline(f["title"])}</div>',
        f'<div style="line-height:1.55;margin:0 0 10px">{_inline(f["detail"])}</div>',
    ]
    for label, key in (("Evidence", "evidence"), ("Fix", "suggested_fix")):
        if f.get(key):
            rows.append(
                f'<div style="line-height:1.5;margin:0 0 8px;font-size:14px">'
                f'<b>{label}:</b> <span style="color:#475467">{_inline(f[key])}</span></div>'
            )
    if f.get("pr_url"):
        url = html.escape(f["pr_url"])
        rows.append(f'<div style="font-size:14px"><b>PR:</b> <a href="{url}">{url}</a></div>')
    return (
        '<div style="border:1px solid #eaecf0;border-radius:10px;padding:16px 18px;'
        f'margin:0 0 14px">{"".join(rows)}</div>'
    )


def render_html(results: list[RepoResult], *, day: date | None = None) -> str:
    """The report as a self-contained HTML email, with inline styles for Gmail."""
    day = day or date.today()
    total_cost = sum(r.cost_usd for r in results)
    parts = [
        f'<div style="font-family:{FONT};color:#101828;font-size:15px;max-width:720px;'
        'margin:0 auto;padding:24px">',
        f'<h1 style="font-size:22px;margin:0 0 4px">repo-sentinel report {day.isoformat()}</h1>',
        f'<p style="color:#667085;margin:0 0 20px">{html.escape(summary_line(results))}. '
        f"Cost ${total_cost:.2f}.</p>",
    ]
    for r in results:
        new = sum(f.get("seen") == "new" for f in r.findings)
        counts = f"{len(r.findings)} findings" + (f", {new} new" if new else "")
        parts += [
            '<div style="margin:28px 0 12px;padding-top:20px;border-top:1px solid #eaecf0">',
            f'<div style="margin:0 0 6px">{_pill(r.status, STATUS_COLOR[r.status])}'
            f'<span style="color:#667085;font-size:13px;margin-left:8px">{counts}</span></div>',
            f'<h2 style="font-size:18px;margin:0 0 8px">{html.escape(r.repo)}</h2>',
            f'<p style="line-height:1.55;margin:0 0 16px;color:#344054">'
            f"{_inline(r.error or r.summary)}</p></div>",
        ]
        parts += [_finding_html(f) for f in r.findings]
        if r.resolved:
            items = "".join(
                f'<li style="margin:0 0 4px">{_inline(f["title"])}</li>' for f in r.resolved
            )
            parts.append(
                '<div style="background:#ecfdf3;border-radius:10px;padding:14px 18px;'
                'margin:0 0 14px"><div style="font-weight:600;color:#067647;margin:0 0 6px">'
                f'Resolved since last run</div><ul style="margin:0;padding-left:20px">{items}'
                "</ul></div>"
            )
    parts.append("</div>")
    return "".join(parts)
