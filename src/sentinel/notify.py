"""Delivery channels. Each one is optional and failures are logged, not raised."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import smtplib
import subprocess
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from sentinel.gh import api

DIGEST_LABEL = "sentinel-digest"


def desktop(title: str, body: str, report_path: Path) -> None:
    if not shutil.which("notify-send") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return
    subprocess.run(
        ["notify-send", "--app-name=repo-sentinel", title, f"{body}\n\n{report_path}"],
        check=False,
    )


async def github_issue(repo: str, markdown: str) -> str:
    """Create or update the single open digest issue in `repo`. Returns its URL."""
    issues = await api(f"repos/{repo}/issues?state=open&labels={DIGEST_LABEL}&per_page=1")
    if issues:
        issue = await api(
            f"repos/{repo}/issues/{issues[0]['number']}", method="PATCH", body={"body": markdown}
        )
    else:
        with contextlib.suppress(Exception):  # label may already exist
            await api(
                f"repos/{repo}/labels",
                method="POST",
                body={"name": DIGEST_LABEL, "color": "3C6EB4"},
            )
        issue = await api(
            f"repos/{repo}/issues",
            method="POST",
            body={"title": "repo-sentinel digest", "body": markdown, "labels": [DIGEST_LABEL]},
        )
    return issue["html_url"]


def telegram(text: str) -> None:
    token = os.environ["SENTINEL_TELEGRAM_TOKEN"]
    chat_id = os.environ["SENTINEL_TELEGRAM_CHAT_ID"]
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(
            {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30).close()


def email(cfg: dict[str, Any], subject: str, markdown: str) -> None:
    user = os.environ["SENTINEL_SMTP_USER"]
    to = os.environ.get("SENTINEL_EMAIL_TO") or cfg.get("to") or user
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(markdown)
    with smtplib.SMTP_SSL(cfg["smtp_host"], cfg.get("smtp_port", 465), timeout=30) as smtp:
        smtp.login(user, os.environ["SENTINEL_SMTP_PASSWORD"])
        smtp.send_message(msg)
