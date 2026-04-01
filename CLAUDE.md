# Claude Code Instructions

## Dev Server

See [.ai/dev-server.md](.ai/dev-server.md) for full details. Key commands:
- Restart after code changes: `be-conductor restart` (warns about active sessions)
- Stop: `be-conductor shutdown` (warns about active sessions)
- Status: `be-conductor status`
- **Always check for active sessions before restarting.** Restarting kills all in-memory sessions. Use `be-conductor restart` (not `systemctl --user restart be-conductor`) so you get a warning. Pass `-f` to skip the prompt.

### Two venvs — know which is which
- **Project `.venv`** (`<repo>/.venv/bin/python3`) — used by the running server process. Code changes are live (editable install), but **version metadata is cached**. After bumping the version in `pyproject.toml`, refresh it: `.venv/bin/pip install -e .`
- **pipx venv** (`~/.local/share/pipx/venvs/be-conductor/`) — provides the `be-conductor` CLI in PATH. Update with: `pipx install -e . --force`
- After a version bump, update the pipx venv (`pipx install -e . --force`) and restart (`be-conductor restart -f`). The project `.venv` may not exist — if so, pipx is the only venv.

## Git Commits

- Do NOT include `Co-Authored-By` lines in commit messages
- Do NOT reference Claude, AI, or any assistant in commits
- Write commit messages as if a human developer wrote them

## Releases

- Repo: `somniacs/be-conductor` (public, GitHub)
- To create a release: `gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."`
- **Always include a changelog link** at the bottom of the release notes: `Full changelog: [CHANGELOG.md](https://github.com/Somniacs/be-conductor/blob/master/CHANGELOG.md)`
- A GitHub Action (`.github/workflows/release.yml`) automatically builds and attaches `be-conductor.tar.gz` and `be-conductor.zip` on every release publish
- Install URLs always point to the latest release — no manual upload needed:
  - Linux/macOS: `https://github.com/somniacs/be-conductor/releases/latest/download/be-conductor.tar.gz`
  - Windows: `https://github.com/somniacs/be-conductor/releases/latest/download/be-conductor.zip`
- Version is defined in one place: `pyproject.toml`. The backend reads it via `importlib.metadata` and the frontend fetches it from `/info`. After updating `pyproject.toml`, refresh the pipx venv (see "Two venvs" above) and restart before tagging
- **After committing a version bump, always run `pipx install -e . --force && be-conductor restart -f`** — don't wait for the user to ask

## Changelog Style

Write changelog entries for **end users**, not developers. Describe what the user sees and can do, not how it's implemented internally.

- **Good**: "You can now type `/stats` to see session statistics — token usage, cost, duration, and context window fill level"
- **Bad**: "Added `_showStatsPopup()` function that fetches from `/sessions/{id}` API and renders a modal with `position:fixed`"

Rules:
- Lead with the user benefit, not the technical mechanism
- No function names, variable names, CSS properties, or internal details
- No line numbers, file paths, or class names
- Keep each entry to 1–2 sentences
- Group related small fixes into one entry when possible
- Use "Fixed:" prefix only when something was broken before — don't list improvements as fixes
