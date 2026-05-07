# Using OpenCode with be-conductor

be-conductor's structured agent view (chat bubbles, tool calls, streaming, cost reporting) works with two coding agents: **Claude** (via the Claude Agent SDK) and **OpenCode** (via the `opencode-ai` Python SDK).

OpenCode is an open-source coding agent that routes to many models — OpenAI's GPT family, Codex variants, Google's Gemini, Anthropic's Claude (via API), and several free / hosted options through OpenCode Zen. Picking one OpenCode adapter inside be-conductor gives you all of those models in the same GUI.

This guide covers:

1. [Installing OpenCode](#1-installing-opencode)
2. [Connecting a model account](#2-connecting-a-model-account)
3. [Running OpenCode locally with be-conductor](#3-running-opencode-locally-with-be-conductor)
4. [Connecting be-conductor to a remote OpenCode server](#4-connecting-be-conductor-to-a-remote-opencode-server)
5. [Creating a session in the dashboard](#5-creating-a-session-in-the-dashboard)
6. [What works, what's missing](#6-what-works-whats-missing)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Installing OpenCode

OpenCode is a separate program from be-conductor. You install it once on whichever machine you want the agent to run on (usually the same machine as be-conductor for the simple case; can be a remote AI workstation for the advanced case).

### Linux / macOS

```bash
curl -fsSL https://opencode.ai/install | bash
```

This drops the binary into `~/.opencode/bin/opencode` and prints a shell-init line you can add to your `~/.bashrc` / `~/.zshrc` so `opencode` is on your `$PATH`.

### Alternatives

If you already use `npm` or `bun`:

```bash
npm i -g opencode-ai
# or
bun i -g opencode-ai
```

### Windows

Use Scoop, Chocolatey, or WSL — see [opencode.ai](https://opencode.ai/) for current instructions. WSL is the simplest path if you're already running be-conductor in WSL.

### Verify

```bash
opencode --version
```

You should see something like `1.14.39`. Make sure the version is recent — older builds have an SSE-stream behaviour be-conductor depends on.

---

## 2. Connecting a model account

OpenCode talks to OpenAI, Google, Anthropic, etc. You authenticate each provider once. be-conductor never touches credentials — they live entirely in OpenCode's own auth store (`~/.local/share/opencode/auth.json` on Linux/macOS).

### List available providers

```bash
opencode providers list
```

This shows which providers OpenCode knows about and which ones you've already authenticated.

### Sign in to a provider

OAuth (recommended for ChatGPT / Claude / Google accounts you already use):

```bash
opencode providers login openai
opencode providers login google
opencode providers login anthropic
```

This opens a browser. Sign in, grant access, you're done.

API key (if you have one):

```bash
opencode providers login openai
# Pick "API key" when prompted, then paste it.
```

### Which models will I have access to?

That depends on your account tier. A ChatGPT (consumer) account has access to **gpt-5.5**, **gpt-5.4**, **gpt-5.3-codex**, etc., but **not** the `-pro` variants — those need an API-key tier or higher subscription. If be-conductor shows the model in the picker but the agent fails with a `ProviderAuthError` ("model is not supported when using Codex with a ChatGPT account"), that's your account tier, not a be-conductor bug.

### Verify

```bash
opencode models
```

You should see one line per `provider/model` you have access to, e.g.:

```
openai/gpt-5.5
openai/gpt-5.5-fast
openai/gpt-5.3-codex
openai/gpt-5.3-codex-spark
google/gemini-2.5-pro
…
```

If a model you expect to see isn't there, run `opencode providers login <provider>` to refresh that provider's token.

---

## 3. Running OpenCode locally with be-conductor

The simplest setup: OpenCode runs as a server on the same machine as be-conductor. be-conductor connects to `http://127.0.0.1:7798`.

### Auto-start (default)

You don't have to do anything. The first time be-conductor needs OpenCode, it spawns `opencode serve --port 7798 --hostname 127.0.0.1` for you and re-uses that server for all subsequent sessions.

### Manual start

If you'd rather control the server yourself (logs, restart, etc.):

```bash
opencode serve --port 7798 --hostname 127.0.0.1
```

Leave it running in a terminal. be-conductor will detect it and skip the auto-start.

### Disable auto-start

Set `BC_OPENCODE_AUTOSTART=false` in be-conductor's environment. Then you must run `opencode serve` yourself; be-conductor will refuse to spawn it.

---

## 4. Connecting be-conductor to a remote OpenCode server

If you want to run OpenCode on a different machine — a beefy AI workstation, a team server, a cloud box — point be-conductor at it via env var:

```bash
BC_OPENCODE_URL=http://aiworkstation.local:7798 be-conductor up
```

For non-localhost servers, set a password on the OpenCode side:

```bash
# On the OpenCode server:
OPENCODE_SERVER_PASSWORD=mysecret opencode serve --port 7798 --hostname 0.0.0.0
```

And tell be-conductor:

```bash
BC_OPENCODE_URL=http://aiworkstation.local:7798 BC_OPENCODE_PASSWORD=mysecret be-conductor up
```

be-conductor sends the password as an `Authorization: Bearer …` header.

### TLS

OpenCode itself doesn't terminate TLS. For HTTPS, put it behind a reverse proxy (nginx, Caddy, Tailscale Funnel) and point `BC_OPENCODE_URL` at the HTTPS endpoint.

### Tailscale / VPN

Easiest pattern: install Tailscale on both machines, run OpenCode on the workstation bound to the Tailscale interface, and use the Tailscale hostname in `BC_OPENCODE_URL`. No public exposure, no manual cert setup.

---

## 5. Creating a session in the dashboard

Once OpenCode is running and authenticated:

1. Open the be-conductor dashboard (default `http://127.0.0.1:7777`).
2. Click **+ New Session**.
3. Toggle session type to **Agent**.
4. The **Agent** dropdown lists Claude alongside every model your local OpenCode is authenticated for — `OpenCode • OpenAI / gpt-5.5`, `OpenCode • OpenAI / gpt-5.3-codex`, `OpenCode • Google / Gemini 2.5 Pro`, etc.
5. Pick a model.
6. Pick a working directory.
7. Click **Run**.

The session opens in the same chat-bubble view used for Claude. The model name is shown as a watermark at the top of the message field.

Mid-session model switches are supported via the model picker in the agent view (subject to the underlying model being available on your account).

---

## 6. What works, what's missing

Things that work the same as Claude:

- Streaming text and reasoning
- Tool calls (bash, file edits, etc.) shown live with their output
- Cost and token tracking per turn
- Context-window indicator (computed from each turn's input tokens vs. the model's documented context limit)
- Image and file attachments — paste/drop a screenshot, vision-capable models see it directly; text files inline; binary files are saved to a temp dir for the agent to read via tools
- Session resume across be-conductor restarts
- Model name shown in the welcome header and as a watermark pill
- Provider auth errors (model rejected by OpenAI etc.) appear as red error blocks in the chat instead of an empty bubble

Things that are deliberately disabled for OpenCode sessions:

- **Permission-mode popup** (`Ask Mode` / `Plan Mode` / etc.) — Claude-specific concept. OpenCode has its own per-agent permission system configured server-side.
- **Effort dial** — Claude-specific.
- **Adaptive thinking switch** — Claude-specific.
- **Clone session** — OpenCode's API doesn't expose a fork-conversation operation. The Clone button is hidden on OpenCode sessions in the dashboard, JetBrains, and VSCode plugins.

---

## 7. Troubleshooting

**"opencode binary not found in PATH"**
You haven't installed OpenCode, or the install dropped it somewhere not on your `$PATH`. Run `which opencode` to check. If it's at `~/.opencode/bin/opencode`, add `export PATH="$HOME/.opencode/bin:$PATH"` to your shell rc.

**Sessions answer with empty bubbles + zero cost**
Either the model rejected the call (look for a red error message in the chat — it's usually a tier issue: `"…not supported when using Codex with a ChatGPT account"`), or OpenCode isn't reachable. Check `curl http://127.0.0.1:7798/session` returns JSON.

**The Agent dropdown only shows "Claude"**
Two reasons:
- OpenCode isn't running. Either auto-start was disabled, or the server crashed. Run `opencode serve --port 7798 --hostname 127.0.0.1` manually.
- OpenCode is running but you haven't authenticated any provider. Run `opencode providers list` to check, then `opencode providers login <provider>` for whichever you want.

**"gpt-5.5-pro is not supported when using Codex with a ChatGPT account"**
Your authentication tier doesn't include that model. Switch to a model your account does have (typically `gpt-5.5`, `gpt-5.5-fast`, `gpt-5.3-codex`, etc.).

**Tools run in the wrong directory**
Each session is scoped to the cwd you picked in the new-session dialog. If you didn't pick one, it falls back to whichever directory you started be-conductor in. Re-create the session with the correct cwd.

**Multiple sessions in different directories interfere with each other**
This was a real bug fixed in `0.3.54`. Make sure you're on a recent be-conductor build; older builds opened one SSE subscriber per session, which OpenCode 1.14.39 starves down to one. Modern be-conductor uses one shared subscriber per (server, directory).

**OpenCode upgrades break compatibility**
The `opencode-ai` Python SDK is alpha-versioned and tracks OpenCode's API. If a major OpenCode upgrade lands and you see auth or schema errors, update be-conductor to a release with a refreshed SDK pin. The SDK is installed from `git+https://github.com/anomalyco/opencode-sdk-python.git@next` (PyPI's release lags); reinstall the be-conductor pipx with `pipx install -e '.[opencode]' --force` to refresh.

---

**See also:** [Quick Start Guide](intro.md) · [README](../README.md)
