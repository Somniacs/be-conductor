# Using ACP agents with be-conductor

be-conductor's structured agent view — chat bubbles, tool calls, live streaming, attachments — works with three kinds of coding agent:

- **Claude (native)** — Anthropic Claude via the Claude Agent SDK. The most capable path.
- **OpenCode** — the open-source agent via the `opencode-ai` SDK. See [docs/opencode.md](opencode.md).
- **ACP agents** — any agent that speaks the **Agent Client Protocol**. This guide covers these.

The [Agent Client Protocol](https://agentclientprotocol.com) (ACP) is an open standard for how editors and coding agents talk to each other. Because it's a *protocol*, one integration in be-conductor reaches many agents. be-conductor ships three out of the box:

- **ACP: Claude** — Claude Code via the ACP adapter
- **ACP: Codex** — OpenAI Codex via the ACP adapter
- **ACP: Gemini** — Google's Gemini CLI, which speaks ACP natively

This guide covers:

1. [When to use an ACP agent](#1-when-to-use-an-acp-agent)
2. [Prerequisites](#2-prerequisites)
3. [Creating an ACP session](#3-creating-an-acp-session)
4. [What works, what's missing](#4-what-works-whats-missing)
5. [Troubleshooting](#5-troubleshooting)

---

## 1. When to use an ACP agent

Pick **Claude (native)** when you want Claude with everything be-conductor can offer — effort levels, the thinking control, plan review, the context ring, cost tracking. The native path is and stays the richest.

Pick an **ACP agent** when you want to drive **Codex** or **Gemini** through the same be-conductor chat view, or when you want Claude Code over the portable protocol. ACP is the breadth layer: more agents, one consistent UI. The trade-off is that ACP standardises only what's common across agents — see [section 4](#4-what-works-whats-missing) for what that means in practice.

## 2. Setup — the easy way

Run one command:

```
be-conductor setup-acp
```

It checks your environment, asks which ACP agents you want (Claude / Codex / Gemini), downloads their adapters so the first session starts instantly, and remembers your choice. For a non-interactive install:

```
be-conductor setup-acp --agents claude,codex --yes
```

To check what's installed and what's missing at any time:

```
be-conductor doctor
```

`doctor` reports your Node.js version, the agent CLIs, and per-agent readiness — with the exact fix command for anything that's not ready.

### What setup-acp needs

ACP agents run as small adapter programs that be-conductor launches on demand. Two things must be in place on the machine running be-conductor:

**Node.js 20 or newer.** The adapters are npm packages. `setup-acp` and `doctor` both check this; if Node is missing or too old they print the install command for your OS (`winget install OpenJS.NodeJS` on Windows, `brew install node` on macOS, your package manager on Linux). be-conductor does not install Node itself.

**The agent's own CLI, signed in.** Each ACP adapter wraps the agent's normal CLI and inherits its login:

- **ACP: Claude** — sign in to Claude Code (`claude` CLI) as you normally would.
- **ACP: Codex** — sign in to the Codex CLI with your ChatGPT account or API key.
- **ACP: Gemini** — sign in to the Gemini CLI (`gemini`).

If the underlying CLI works in your terminal, the ACP agent works in be-conductor. `doctor` shows which CLIs it found.

> If you skip `setup-acp`, ACP still works — the adapter just downloads on the first session instead (a slower first start). If Node.js is missing or too old, the session fails immediately with a clear message telling you to run `setup-acp`.

## 3. Creating an ACP session

1. Open the new-session dialog (the **+** in the dashboard, or **New Session** in the JetBrains / VSCode plugin).
2. Set the session type to **Agent**.
3. In the **Agent** picker, choose **ACP: Claude**, **ACP: Codex**, or **ACP: Gemini**.
4. Pick a working directory, optionally tick **Isolate with git worktree**, and run.

The first prompt may take a few extra seconds while `npx` fetches the adapter. After that the session behaves like any other agent session.

The same Agent picker is available in the JetBrains and VSCode plugins — you can launch an ACP session straight from the IDE.

## 4. What works, what's missing

**Works the same as Claude:**

- Chat bubbles and live token-by-token streaming
- Tool calls — shown as discrete steps, with their output
- Permission prompts — when the agent wants to run a tool, you approve or reject it in the chat
- Attachments — drop or paste an image (vision-capable agents see it directly), a text file (inlined into the prompt), or any other file (saved to disk; the agent reads it with its file tool)
- Worktree isolation — tick the worktree checkbox and the ACP agent runs on its own branch; file edits and commands all happen inside the worktree
- Slash commands — the agent's own commands show up as they become available
- **`/btw`** — the side-channel "by the way" question works for ACP agents; the question and answer show transiently and aren't written into the conversation history
- **Resume** — for agents that support it (Claude, Gemini), an exited session can be resumed and the agent replays the conversation. Your chat history is always kept regardless

**Not available for ACP sessions** (ACP doesn't standardise these):

- The context-window ring and cost figures — ACP has no usage reporting, so those widgets are hidden
- Claude-only controls — the effort dial, the adaptive-thinking control, the "Ask Mode" plan popup. These are native-Claude features; use **Claude (native)** if you want them
- Clone / fork — ACP has no fork operation, so the Clone button doesn't appear on ACP sessions

This is the deliberate split: **ACP gives breadth** (Codex, Gemini, and more, in one UI), **native Claude gives depth**.

## 5. Troubleshooting

**First step for any ACP problem: run `be-conductor doctor`.** It tells you, in one place, whether Node.js, npx, and each agent CLI are ready, and prints the fix for anything that isn't.

**An ACP session fails immediately with "needs Node.js ≥ 20"**
The be-conductor server is using an old (or missing) Node.js. Install Node 20+, then **restart the be-conductor server** so it picks up the new Node on its `PATH`. If you have several Node versions (e.g. via nvm), make sure the one on the server's `PATH` is 20+. Re-check with `be-conductor doctor`.

**The first prompt hangs for a long time**
`npx` is downloading the adapter from npm. Run `be-conductor setup-acp` once up front to avoid this — it pre-downloads the adapters. If a download never completes, check that the be-conductor machine has network access to `registry.npmjs.org`.

**The agent says it isn't authenticated**
The ACP adapter inherits the underlying CLI's login. Open a terminal on the be-conductor machine and confirm the agent's own CLI (`claude`, `codex`, `gemini`) is signed in there. `be-conductor doctor` shows which CLIs it found.

**Resume is missing for an ACP session**
Not every ACP agent supports session loading. When an agent doesn't, be-conductor hides the Resume action — but the chat history is still kept and shown. Starting the agent again begins a fresh session.

**Tool edits aren't showing in the worktree diff**
ACP agents perform file edits by calling back to be-conductor, which writes them inside the session's working directory. Make sure the session was created with a directory inside the git repo (and the worktree checkbox ticked, if you want isolation).
