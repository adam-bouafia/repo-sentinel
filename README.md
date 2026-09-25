# repo-sentinel

An agent built on the Claude Agent SDK that audits a list of GitHub repositories and reports what is broken, outdated or drifting from upstream. For small mechanical fixes it opens a pull request.

For each repo it:

- investigates failed workflow runs on the default branch and finds the root cause
- compares pinned dependencies, GitHub Actions and platform versions with current upstream releases
- reads open Dependabot alerts and open pull requests
- checks project-specific points you describe in the config (store listings, target platform versions)
- opens fix PRs on `sentinel/*` branches for low-risk changes such as version bumps

Results go to a Markdown report, a desktop notification, a GitHub issue digest, and optionally Telegram and email.

## Safety model

The agent has no shell and no local file access. Its only tools are:

| Tool | Access |
|---|---|
| `repo_overview`, `list_files`, `read_file`, `failed_run_log` | read-only, bound to the one repo under audit |
| `open_fix_pr` | creates a new `sentinel/*` branch and a PR |
| `WebFetch`, `WebSearch` | read-only upstream lookups |

The PR rules are enforced in code, not in the prompt: a new branch only, never the default branch, never a merge, at most 10 files, relative paths only, no duplicate PR while a sentinel PR with the same title or branch is open. `--no-pr` or `allow_prs = false` turns PRs off entirely.

Each repo gets its own session with `permission_mode = "dontAsk"`, an empty working directory, no host settings (`setting_sources = []`), a turn limit and a hard dollar budget (`max_budget_usd`).

## Setup

Requirements: Python 3.12+, the `gh` CLI logged in (`gh auth login`), and Claude credentials. Locally the bundled Claude Code CLI reuses your Claude Code login; otherwise set `ANTHROPIC_API_KEY`.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Edit `sentinel.toml`: add one `[[repos]]` block per repository, with `notes` describing what "up to date" means for it.

## Usage

```bash
.venv/bin/sentinel run                      # all repos, PRs and notifications as configured
.venv/bin/sentinel run -r repo-name -v      # one repo, print tool calls
.venv/bin/sentinel run --no-pr --no-notify  # dry run: report file only
```

Reports are written to `reports/YYYY-MM-DD.md`.

## Scheduling

### systemd (local, weekly)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/repo-sentinel.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now repo-sentinel.timer
systemctl --user start repo-sentinel.service   # run once now
journalctl --user -u repo-sentinel -f
```

The service expects the project at `~/Documents/repo-sentinel` with the venv in `.venv`.

### GitHub Actions (cloud, weekly)

`.github/workflows/sentinel.yml` runs every Monday and on manual dispatch. It commits the report to `reports/` and updates the digest issue. Repository secrets:

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API access |
| `SENTINEL_GH_TOKEN` | fine-grained PAT with read access to the watched repos, plus contents/pull-requests write if PRs are on and issues write on this repo. The default `GITHUB_TOKEN` only covers this repo. |
| `SENTINEL_TELEGRAM_TOKEN`, `SENTINEL_TELEGRAM_CHAT_ID` | optional, Telegram |
| `SENTINEL_SMTP_USER`, `SENTINEL_SMTP_PASSWORD` | optional, email |

## Notifications

| Channel | Config | Needs |
|---|---|---|
| Desktop | `[notify.desktop]` | `notify-send` and a session bus (skipped in CI) |
| GitHub issue | `[notify.github_issue]` | one open issue labelled `sentinel-digest`, updated each run |
| Telegram | `[notify.telegram]` | `SENTINEL_TELEGRAM_TOKEN`, `SENTINEL_TELEGRAM_CHAT_ID` |
| Email | `[notify.email]` | `SENTINEL_SMTP_USER`, `SENTINEL_SMTP_PASSWORD` (Gmail: an app password) |

## Development

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest
```

## License

MIT
