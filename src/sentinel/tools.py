"""Per-repo tools exposed to the agent as an in-process MCP server.

Every tool is a closure over a single repo, so an audit of repo A cannot read from
or write to repo B. Write access is limited to `open_fix_pr`, which enforces the
autonomy rules in code: new sentinel/* branch only, never the default branch,
never merges, no duplicate PRs, every bumped version must exist upstream, and an
independent reviewer must approve the diff.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from sentinel.gh import GhError, api, gh

SERVER_NAME = "repo"
BRANCH_PREFIX = "sentinel/"
MAX_PR_FILES = 10
MAX_FILE_CHARS = 100_000
MAX_LOG_CHARS = 15_000
MAX_DIFF_CHARS = 60_000
DIFF_CONTEXT = 20  # lines around each change, so the reviewer sees e.g. workflow triggers

# Takes the PR as text (title, body, diff), returns (approved, reason).
Reviewer = Callable[[str], Awaitable[tuple[bool, str]]]

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _text(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return {"content": [{"type": "text", "text": text}]}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "fix"


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "repo-sentinel"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


async def lookup_upstream_version(source: str, name: str) -> dict[str, Any]:
    """Latest published version of a package or project.

    Args:
        source: "pypi", "npm" or "github" (name is owner/repo; latest release, else newest tag).
        name: package or repository name.

    Raises:
        ValueError: for an unknown source.
    """
    quoted = urllib.parse.quote(name, safe="@/")
    if source == "pypi":
        info = (await asyncio.to_thread(_get_json, f"https://pypi.org/pypi/{quoted}/json"))["info"]
        return {"version": info["version"], "requires_python": info.get("requires_python")}
    if source == "npm":
        data = await asyncio.to_thread(_get_json, f"https://registry.npmjs.org/{quoted}/latest")
        return {"version": data["version"]}
    if source == "github":
        try:
            rel = await api(f"repos/{name}/releases/latest")
            return {"version": rel["tag_name"], "published_at": rel["published_at"]}
        except GhError:
            tags = await api(f"repos/{name}/tags?per_page=1")
            return {"version": tags[0]["name"] if tags else None, "from": "tags"}
    raise ValueError(f"unknown source {source!r}")


def _url_exists(url: str) -> bool:
    try:
        _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    return True


async def version_exists(source: str, name: str, version: str) -> bool:
    """Whether `version` of a package or project is actually published.

    Why: a model can name a version that does not exist; a bump to it breaks the
    build, and an unknown package name is free for anyone to register.

    Args:
        source: "pypi", "npm" or "github" (version is a tag, e.g. v7 or v7.0.1).
        name: package or owner/repo.
        version: the exact version the PR pins.

    Raises:
        ValueError: for an unknown source.
    """
    quoted = urllib.parse.quote(name, safe="@/")
    v = urllib.parse.quote(version, safe="")
    if source == "pypi":
        return await asyncio.to_thread(_url_exists, f"https://pypi.org/pypi/{quoted}/{v}/json")
    if source == "npm":
        return await asyncio.to_thread(_url_exists, f"https://registry.npmjs.org/{quoted}/{v}")
    if source == "github":
        try:
            await api(f"repos/{name}/git/ref/tags/{v}")
        except GhError:
            return False
        return True
    raise ValueError(f"unknown source {source!r}")


def unified_diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=DIFF_CONTEXT,
        )
    )


async def _safe(coro: Any, fallback: Any) -> Any:
    try:
        return await coro
    except GhError as e:
        return {"unavailable": str(e)} if fallback is None else fallback


def build_repo_server(repo: str, *, allow_prs: bool, reviewer: Reviewer | None = None) -> Any:
    """Create the MCP server bound to `repo`.

    Args:
        repo: owner/name of the repository under audit.
        allow_prs: when False, open_fix_pr refuses and the agent only reports.
        reviewer: must approve each PR diff before it is opened; None skips review.
    """
    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="0.1.0",
        tools=build_repo_tools(repo, allow_prs=allow_prs, reviewer=reviewer),
    )


def build_repo_tools(repo: str, *, allow_prs: bool, reviewer: Reviewer | None = None) -> list[Any]:
    """The tool objects behind build_repo_server, exposed for tests."""

    @tool(
        "repo_overview",
        "Snapshot of the repository: metadata, default branch, latest release and tags, "
        "recent workflow runs (all branches and tags), open pull requests, open Dependabot "
        "alerts and whether Dependabot/Renovate is configured. Call this first.",
        {},
        annotations=READ_ONLY,
    )
    async def repo_overview(_args: dict[str, Any]) -> dict[str, Any]:
        meta = await api(f"repos/{repo}")
        branch = meta["default_branch"]
        runs, release, tags, prs, alerts, dependabot_cfg, renovate_cfg = await asyncio.gather(
            _safe(api(f"repos/{repo}/actions/runs?per_page=20"), {}),
            _safe(api(f"repos/{repo}/releases/latest"), None),
            _safe(api(f"repos/{repo}/tags?per_page=5"), []),
            _safe(api(f"repos/{repo}/pulls?state=open&per_page=30"), []),
            _safe(api(f"repos/{repo}/dependabot/alerts?state=open&per_page=50"), None),
            _safe(api(f"repos/{repo}/contents/.github/dependabot.yml"), None),
            _safe(api(f"repos/{repo}/contents/renovate.json"), None),
        )
        return _text(
            {
                "repo": repo,
                "description": meta.get("description"),
                "default_branch": branch,
                "language": meta.get("language"),
                "archived": meta.get("archived"),
                "pushed_at": meta.get("pushed_at"),
                "open_issues": meta.get("open_issues_count"),
                "latest_release": None
                if not release or "unavailable" in release
                else {k: release.get(k) for k in ("tag_name", "published_at", "html_url")},
                "tags": [t["name"] for t in tags],
                "workflow_runs": [
                    {
                        k: r.get(k)
                        for k in (
                            "id",
                            "name",
                            "event",
                            "status",
                            "conclusion",
                            "head_branch",
                            "created_at",
                            "html_url",
                        )
                    }
                    for r in runs.get("workflow_runs", [])
                ],
                "open_pull_requests": [
                    {
                        "number": p["number"],
                        "title": p["title"],
                        "branch": p["head"]["ref"],
                        "author": p["user"]["login"],
                        "draft": p["draft"],
                        "created_at": p["created_at"],
                    }
                    for p in prs
                ],
                "dependabot_alerts": alerts
                if not isinstance(alerts, list)
                else [
                    {
                        "package": a["dependency"]["package"]["name"],
                        "severity": a["security_advisory"]["severity"],
                        "summary": a["security_advisory"]["summary"],
                        "manifest": a["dependency"]["manifest_path"],
                    }
                    for a in alerts
                ],
                "dependabot_configured": isinstance(dependabot_cfg, dict)
                and "unavailable" not in dependabot_cfg,
                "renovate_configured": isinstance(renovate_cfg, dict)
                and "unavailable" not in renovate_cfg,
            }
        )

    @tool(
        "failed_run_log",
        "Log output of the failed steps of a workflow run (tail, truncated).",
        {"run_id": int},
        annotations=READ_ONLY,
    )
    async def failed_run_log(args: dict[str, Any]) -> dict[str, Any]:
        try:
            log = await gh("run", "view", str(args["run_id"]), "-R", repo, "--log-failed")
        except GhError as e:
            return _error(f"could not fetch log: {e}")
        return _text(log[-MAX_LOG_CHARS:] or "(no failed-step log)")

    @tool(
        "list_files",
        "List file paths in the default branch, optionally filtered by a path prefix.",
        {"prefix": str},
        annotations=READ_ONLY,
    )
    async def list_files(args: dict[str, Any]) -> dict[str, Any]:
        meta = await api(f"repos/{repo}")
        tree = await api(f"repos/{repo}/git/trees/{meta['default_branch']}?recursive=1")
        prefix = args.get("prefix", "")
        paths = [
            e["path"] for e in tree["tree"] if e["type"] == "blob" and e["path"].startswith(prefix)
        ]
        return _text("\n".join(paths[:2000]) or "(no files)")

    @tool(
        "read_file",
        "Read a file from the default branch.",
        {"path": str},
        annotations=READ_ONLY,
    )
    async def read_file(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = await api(f"repos/{repo}/contents/{args['path']}")
        except GhError as e:
            return _error(f"cannot read {args['path']}: {e}")
        if not isinstance(data, dict) or data.get("type") != "file":
            return _error(f"{args['path']} is not a file")
        text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return _text(text[:MAX_FILE_CHARS])

    @tool(
        "upstream_version",
        "Latest published version of a dependency. source is pypi, npm or github "
        "(name = owner/repo, e.g. actions/checkout). Much cheaper than WebFetch; use it "
        "for every version comparison it covers.",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["pypi", "npm", "github"]},
                "name": {"type": "string"},
            },
            "required": ["source", "name"],
        },
        annotations=READ_ONLY,
    )
    async def upstream_version(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _text(await lookup_upstream_version(args["source"], args["name"]))
        except Exception as e:  # noqa: BLE001 - surface lookup failures to the agent
            return _error(f"lookup failed: {type(e).__name__}: {e}")

    @tool(
        "open_fix_pr",
        "Open a pull request with a small, safe fix (e.g. version bump, metadata update). "
        "Creates a new sentinel/* branch from the default branch with one commit containing "
        "the given full file contents. Never pushes to the default branch and never merges. "
        "List every version bump in `bumps`; each is checked to exist upstream. An "
        "independent reviewer sees only the title, body and diff and may reject the PR. "
        "Only use for changes you are confident in; report everything else as a finding.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"],
                    },
                },
                "bumps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string", "enum": ["pypi", "npm", "github"]},
                            "name": {"type": "string"},
                            "version": {"type": "string"},
                        },
                        "required": ["source", "name", "version"],
                    },
                },
            },
            "required": ["title", "body", "files"],
        },
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=True),
    )
    async def open_fix_pr(args: dict[str, Any]) -> dict[str, Any]:
        if not allow_prs:
            return _error("PRs are disabled for this run; report the fix as a finding instead.")
        files = args["files"]
        if not files or len(files) > MAX_PR_FILES:
            return _error(f"a fix PR must change between 1 and {MAX_PR_FILES} files")
        if any(f["path"].startswith("/") or ".." in f["path"].split("/") for f in files):
            return _error("file paths must be relative to the repo root")
        bumps = args.get("bumps", [])
        for b in bumps:
            if not any(b["version"] in f["content"] for f in files):
                return _error(f"bump {b['name']} {b['version']} does not appear in any file")

        branch = f"{BRANCH_PREFIX}{_slug(args['title'])}-{date.today():%Y%m%d}"
        prs = await api(f"repos/{repo}/pulls?state=open&per_page=100")
        for p in prs:
            if p["head"]["ref"].startswith(BRANCH_PREFIX) and (
                p["title"] == args["title"] or p["head"]["ref"] == branch
            ):
                return _error(f"an open sentinel PR already covers this: {p['html_url']}")

        for b in bumps:
            try:
                exists = await version_exists(b["source"], b["name"], b["version"])
            except Exception as e:  # noqa: BLE001 - unverifiable means no PR
                return _error(f"could not verify {b['name']} {b['version']}: {e}")
            if not exists:
                return _error(f"{b['source']} has no {b['name']} {b['version']}; not opening a PR")

        try:
            meta = await api(f"repos/{repo}")
            base = meta["default_branch"]
            diffs = []
            for f in files:
                try:
                    data = await api(f"repos/{repo}/contents/{f['path']}?ref={base}")
                    old = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
                except GhError:
                    old = ""  # new file
                if len(old) >= MAX_FILE_CHARS:
                    return _error(f"{f['path']} is longer than read_file returns; edit it by hand")
                diffs.append(unified_diff(f["path"], old, f["content"]))
            diff = "".join(diffs)
            if not diff:
                return _error("the files are identical to the default branch; nothing to fix")
            if len(diff) > MAX_DIFF_CHARS:
                return _error("the diff is too large for a mechanical fix; report it instead")
            if reviewer:
                approved, reason = await reviewer(
                    f"Title: {args['title']}\n\nBody:\n{args['body']}\n\n"
                    f"Declared bumps: {json.dumps(bumps)}\n\nDiff:\n{diff}"
                )
                if not approved:
                    return _error(f"reviewer rejected the PR: {reason}. Report it as a finding.")

            head = await api(f"repos/{repo}/git/ref/heads/{base}")
            base_sha = head["object"]["sha"]
            base_commit = await api(f"repos/{repo}/git/commits/{base_sha}")
            tree = await api(
                f"repos/{repo}/git/trees",
                method="POST",
                body={
                    "base_tree": base_commit["tree"]["sha"],
                    "tree": [
                        {
                            "path": f["path"],
                            "mode": "100644",
                            "type": "blob",
                            "content": f["content"],
                        }
                        for f in files
                    ],
                },
            )
            commit = await api(
                f"repos/{repo}/git/commits",
                method="POST",
                body={"message": args["title"], "tree": tree["sha"], "parents": [base_sha]},
            )
            await api(
                f"repos/{repo}/git/refs",
                method="POST",
                body={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
            pr = await api(
                f"repos/{repo}/pulls",
                method="POST",
                body={
                    "title": args["title"],
                    "head": branch,
                    "base": base,
                    "body": args["body"] + "\n\n---\nOpened automatically by repo-sentinel. "
                    "Review before merging.",
                },
            )
        except GhError as e:
            return _error(f"failed to open PR: {e}")
        return _text({"pr_url": pr["html_url"], "branch": branch})

    return [repo_overview, failed_run_log, list_files, read_file, upstream_version, open_fix_pr]


TOOL_NAMES = [
    f"mcp__{SERVER_NAME}__{n}"
    for n in (
        "repo_overview",
        "failed_run_log",
        "list_files",
        "read_file",
        "upstream_version",
        "open_fix_pr",
    )
]
