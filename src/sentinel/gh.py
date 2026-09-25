"""Thin async wrapper around the gh CLI.

Why gh instead of a REST client: it is preinstalled on GitHub runners, reuses the
local `gh auth` login, and honours GH_TOKEN in CI with no extra code.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class GhError(RuntimeError):
    pass


async def gh(*args: str, stdin: str | None = None) -> str:
    """Run `gh <args>` and return stdout.

    Raises:
        GhError: on a non-zero exit, with gh's stderr as the message.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
    if proc.returncode != 0:
        raise GhError(err.decode().strip() or f"gh {' '.join(args)} failed")
    return out.decode()


async def api(path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    """Call the GitHub REST API through `gh api` and decode the JSON response."""
    args = ["api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
    if body is not None:
        args += ["--input", "-"]
    out = await gh(*args, stdin=json.dumps(body) if body is not None else None)
    return json.loads(out) if out.strip() else None
