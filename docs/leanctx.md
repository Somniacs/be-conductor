# LeanCTX (optional, per profile)

[LeanCTX](https://leanctx.com) (`lean-ctx`) is a local context-compression
layer for coding agents: an MCP server, a shell hook and a managed
instructions block that make the agent read compressed views of files and
command output. On headless runs that can save a meaningful share of quota.

It is **lossy** — exact edits still need raw reads — so be-conductor treats it
as a per-profile opt-in and never installs it for you. Default: `off`.
Recommended: `active` on headless/worker profiles only; leave your default
login alone.

```bash
be-conductor profile leanctx claude-max5 active
be-conductor profile leanctx claude-max5 off
```

(or **Init LeanCTX** / the LeanCTX field in Settings → Profiles.)

## What be-conductor does

`active` runs `lean-ctx init --agent <claude|codex|opencode>` with the
profile's environment (`CLAUDE_CONFIG_DIR` / `CODEX_HOME` /
`XDG_CONFIG_HOME`), so LeanCTX installs into the profile's config dir.

LeanCTX documents no option for targeting a custom config dir, so nothing is
taken on trust:

- The profile dir is snapshotted before `init`. **`off` undoes exactly what
  `init` changed** (deletes the files it created, restores the ones it
  modified). `lean-ctx uninstall` is never used — it is machine-wide.
- Your **default** agent config (`~/.claude/CLAUDE.md`, `settings.json`,
  `skills/`, `~/.claude.json`, `~/.codex/…`, `~/.config/opencode/…`) is
  snapshotted too. If `init` writes a LeanCTX block there anyway, it is
  reverted immediately, LeanCTX is *not* enabled for the profile, and you are
  told. Unrelated concurrent writes to those files are left alone.
- If `init` changes nothing inside the profile dir, that is reported as a
  failure rather than a silent no-op.

## Shadow mode

`shadow` (measure only) is stored on the profile and sets LeanCTX up exactly
like `active`. LeanCTX's actual shadow switch is machine-wide —

```toml
# ~/.config/lean-ctx/config.toml
[shadow]
enabled = true
```

— so be-conductor does not flip it for you: it would also affect your default
login. Savings are reported by LeanCTX itself (`lean-ctx gain`,
`lean-ctx savings --period week`).
