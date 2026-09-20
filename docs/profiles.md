# Account profiles

A **profile** runs an agent session under its own login or API key instead of
this machine's default account. Typical setup:

| You have | Profile | How it is isolated |
|---|---|---|
| A second Claude subscription | `claude-max5` (backend `claude`) | `CLAUDE_CONFIG_DIR` |
| ChatGPT / Codex subscription | `codex-pro5` (backend `codex`) | `CODEX_HOME` |
| An OpenRouter API key | `openrouter-any` (backend `opencode`) | `XDG_CONFIG_HOME` + `XDG_DATA_HOME`, key from the OS keyring |

Your default login (`~/.claude`, `~/.codex`, `~/.config/opencode`) is never
touched: a profile must live in its own directory (default
`~/.be-conductor/profiles/<name>`, mode 0700) and be-conductor refuses to point
one at a default location. Everything here is opt-in — without a `profiles:`
block in `~/.be-conductor/config.yaml` nothing changes.

## Set up a second Claude subscription

```bash
be-conductor profile add claude-max5 --backend claude --description "Claude Code on the second Max subscription"
be-conductor profile login claude-max5     # runs `claude auth login` under the profile — sign in with the OTHER account
be-conductor profile check claude-max5     # runs a trivial prompt to prove it works
be-conductor run --profile claude-max5 claude review
```

`profile login` opens a normal be-conductor session, so you can also finish the
sign-in **from your phone**: Settings → Profiles → **Login** in the dashboard
opens the same flow in a terminal panel (it prints a URL and takes the code).

## Codex

```bash
be-conductor profile add codex-pro5 --backend codex
be-conductor profile login codex-pro5      # `codex login --device-auth` — device code, phone friendly
```

## OpenCode with an OpenRouter key

```bash
be-conductor profile add openrouter-any --backend opencode \
    --secret OPENROUTER_API_KEY --model openrouter/anthropic/claude-sonnet-4.6 --max-cost 2.0
be-conductor profile set-key openrouter-any OPENROUTER_API_KEY   # prompts; stored in the OS keyring
```

- The key lives **only** in the OS keyring (Secret Service / Keychain /
  Credential Manager) and in the child process environment. It is never
  written to `config.yaml`, and if it ever shows up in a session's output it is
  replaced with `[redacted]` before it reaches the buffer or the browser.
- On a Linux box without a Secret Service the key goes to
  `~/.be-conductor/secrets/` (mode 0600) and the dashboard says so.
- A profile with a declared secret **refuses to start** while the key is
  missing, so it can never silently run on some other credential.
- `--max-cost` stops a headless run once its reported cost exceeds the cap
  (status `failed`, reason `cost_cap`).
- OpenCode keeps its credentials under `XDG_DATA_HOME`, not only
  `XDG_CONFIG_HOME` — the profile redirects both. Side effect: tools the agent
  launches also see the redirected `XDG_CONFIG_HOME` (e.g. `gh` will not find
  its login). Add what you need under the profile's `env:`.

## What a profile does to the environment

```
session env = server env
              − every CLAUDE* variable            (as for all sessions)
              − strip_env                         (default: ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
                                                   CLAUDE_CODE_OAUTH_TOKEN, OPENAI_API_KEY, OPENROUTER_API_KEY)
              + the backend's config-dir variable(s)
              + env
              + secrets (from the keyring, at spawn time)
              + env passed with the request
```

Full config reference:

```yaml
profiles:
  - name: claude-max5                 # ^[a-z0-9_-]+$, unique
    backend: claude                   # claude | codex | opencode | custom
    config_dir: ~/.be-conductor/profiles/claude-max5    # optional, this is the default
    env: {}                           # extra variables
    secrets:                          # values come from the keyring, never from this file
      - env: OPENROUTER_API_KEY
        keyring: be-conductor/openrouter-any            # service/username
    strip_env: [ANTHROPIC_API_KEY, ...]                 # optional override
    model: openrouter/anthropic/claude-sonnet-4.6       # {model} in headless args
    max_cost_usd_per_run: 2.0
    leanctx: off                      # see docs/leanctx.md
    description: "Shown in the dashboard and to MCP clients."
```

`backend: custom` applies only `env` and `secrets`.

## Binding a command to a profile

```yaml
allowed_commands:
  - command: "claude"
    label: "Claude Code"                 # default login, unchanged
  - command: "claude"
    label: "Claude Code — Max5"
    profile: claude-max5
    headless: true                       # see below
```

The quickest way is the dashboard: Settings → **Profiles** → **+ Command** on
the profile's row. The entry is built from the backend — command, resume
behaviour, a label, headless on — and opens for review; press **Save** on the
Agents tab to keep it. Picking a profile in the command editor fills in the
same blanks.

In the dashboard the **+ New** dialog shows a *Profile* dropdown with the
profiles that fit the chosen command; picking the "Max5" entry preselects its
profile. Session cards show the profile as a badge, and stall notifications
name it. Native Claude **GUI** sessions honour a profile too; OpenCode/ACP GUI
sessions do not (they share one agent server) and say so.

## Headless tasks

A command with a `headless` block can be run to completion for its answer:

```bash
be-conductor task "Claude Code — Max5" "summarize the open TODOs in this repo"
be-conductor task claude "fix the failing test" -w --timeout 1200   # in a git worktree
be-conductor task opencode "review src/" --profile openrouter-any --json
be-conductor task "Claude Code — Max5" "plan the refactor" --model claude-fable-5-1
```

`--model` (and the `model` argument of the MCP `run_*` tools) picks the model
for that one run. Order: the run's own model, then `model:` in the command's
headless block, then the profile's `model:`, else the agent's default.

A profile's `model:` is therefore only a fallback for calls that name none —
leave it empty to always choose per run. In the dashboard the field is a
dropdown: `GET /profiles/<name>/models` for a saved profile (the backend's own
listing run with the profile's environment, so a provider it has no key for
does not appear), and `GET /profile-models?backend=&env=` while you are still
adding one — the backend's catalogue narrowed to the providers the named API
key variables unlock.

`headless: true` uses the built-in preset for `claude`, `codex` or `opencode`.
For those three a custom block *refines* the preset — set only what differs
(e.g. just `args` to add a flag) and the rest, including the output parsing,
stays. A full block for another tool looks like this:

```yaml
    headless:
      args: ["-p", "{prompt}", "--output-format", "json"]   # {prompt}, {model}
      format: json            # json | jsonl (event stream) | text   (default: json if a result path is set)
      result_json_path: result
      cost_json_path: total_cost_usd
      tokens_json_path: usage
      error_json_path: is_error                 # truthy → failed
      result_match: {"item.type": "agent_message"}   # jsonl: which events carry the result
      error_match: {"type": "error"}                 # jsonl: which events mean failure
```

A headless run is a normal session: it appears in the sidebar with a status
badge (`running` / `needs input` / `done` / `failed` / `timeout`), can be
watched live, and — if it stops at a prompt — fires the usual notification so
you can answer it from your phone. Finished cards show the cost; ☰ shows the
result. A finished Claude task can be **continued interactively** with ▶ (it
resumes the task's own conversation, under the same profile).

On timeout the whole process tree is killed and whatever was printed is
returned as the result.

## Usage and cost

Every finished headless run appends a line to
`~/.be-conductor/profiles/<name>/ledger.jsonl`.

```bash
be-conductor profile usage openrouter-any
be-conductor profile list                  # login/key status + today / week spend
```

Subscription logins report a *notional* API cost — useful for comparing runs,
not a bill.

## CLI

```
be-conductor profile list|add|edit|remove <name>
be-conductor profile login <name>
be-conductor profile check <name>
be-conductor profile set-key <name> <ENV>
be-conductor profile usage <name>
be-conductor profile leanctx <name> off|shadow|active
be-conductor profile purge                 # delete ALL profiles, logins and stored keys
be-conductor run --profile <name> <command> [session]
be-conductor task <label|command> "<prompt>" [--profile P] [--model M] [--dir D] [-w] [--timeout S] [-d] [--json]
```

## REST

| Method | Endpoint | |
|---|---|---|
| GET | `/profiles` | list with status (never secret values) |
| PUT | `/admin/profiles` | replace the list (localhost or token) |
| POST | `/admin/profiles/{name}/secret` | `{env, value}` → keyring |
| DELETE | `/admin/profiles/{name}/secret/{env}` | |
| POST | `/profiles/{name}/login` | start the login session |
| POST | `/profiles/{name}/check` | trivial prompt, 60 s |
| GET | `/profiles/{name}/usage` | ledger totals |
| POST | `/sessions/run` | new optional fields: `profile`, `label`, `headless`, `prompt`, `model`, `timeout_seconds` |
| GET | `/sessions/{id}/result` | `{task_status, result, cost_usd, …}` (`?tail=N` adds live output) |
