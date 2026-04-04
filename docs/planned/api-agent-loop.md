# GUI Agent: Claude Agent SDK Strategy

## Status: Staying with the Agent SDK

**Decision (April 2026):** We are fully committed to the Claude Agent SDK (`claude_agent_sdk`). Anthropic promises CLI-level performance through the SDK, and it is under rapid active development (daily releases since March 2026). The original plan to replace it with raw `anthropic` API calls is **not the path forward**.

## What the Agent SDK provides

The SDK wraps the Claude Code CLI as a subprocess and gives us:

- **Built-in tools**: Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, AskUserQuestion, Agent, TodoWrite, and more — all executed internally, no implementation needed on our side
- **Auto-compaction**: The CLI auto-compacts when context approaches its limit, emitting `SystemMessage(subtype="compact_boundary")`
- **Session management**: Resume, fork, continue — with JSONL files stored in `~/.claude/projects/`
- **Hooks**: PreToolUse, PostToolUse, PreCompact, Stop, SubagentStart/Stop
- **Budget controls**: `max_budget_usd`, `max_turns`
- **Thinking/effort**: `thinking` config (adaptive, enabled with budget, disabled), `effort` levels
- **Context inspection**: `get_context_usage()` returns per-category breakdown (added in v0.1.52)
- **Subagents**: Spawn sub-agents with fresh context for subtasks
- **MCP servers**: Connect external tools via Model Context Protocol
- **Settings**: Loads CLAUDE.md, skills, slash commands via `setting_sources`

## Current Architecture

File: `be_conductor/sessions/agent_session.py`

```
User prompt → _input_queue → client.query(text) → _stream_response(client)
                                                        │
                                                        ├── AssistantMessage → _emit_event (broadcast + history)
                                                        ├── ResultMessage → _emit_event (turn done)
                                                        ├── SystemMessage → _emit_event (compact_boundary, etc.)
                                                        └── RateLimitEvent → _broadcast_event
```

Key integration points:
- `ClaudeAgentOptions` configured with: cwd, permission_mode, can_use_tool callback, setting_sources, hooks, model, effort, thinking, max_budget_usd
- `_can_use_tool()` callback intercepts ALL tool calls for GUI permission flow
- `_ask_user_hook()` / `_exit_plan_hook()` as PreToolUse hooks for AskUserQuestion and ExitPlanMode
- `_stream_response()` processes AssistantMessage, ResultMessage, SystemMessage, RateLimitEvent
- `_send_btw()` uses standalone `query()` function for ephemeral side questions
- Runtime changes via `client.set_model()`, `client.set_permission_mode()`
- Context inspection via `client.get_context_usage()`

## What we fixed (April 2026)

### Token waste root causes found and addressed:

1. **SDK upgrade 0.1.51 → 0.1.56** (CLI 2.1.85 → 2.1.92) — critical fixes:
   - Auto-compact thrash loop (infinite compact → refill cycle burning API calls)
   - Nested CLAUDE.md re-injected dozens of times in long sessions
   - Prompt cache misses from tool schema bytes changing mid-session
   - Read tool now deduplicates unchanged re-reads
   - Hook output >50K saved to disk instead of injected into context
   - Edit tool uses shorter anchors (fewer output tokens)

2. **No longer overriding CLI defaults** — we previously forced thinking budget and max_budget_usd. Now we only pass effort/thinking/budget to the SDK if explicitly set by the user. The CLI uses its own internal defaults, same as terminal Claude.

3. **Visibility improvements**:
   - `compact_boundary` events shown in timeline ("Context compacted — older messages summarized")
   - Result `subtype` forwarded (error_max_budget_usd, error_max_turns) with clear UI messages
   - Running cost displayed in context indicator tooltip
   - "Context breakdown" popup via `get_context_usage()` — shows per-category token usage, auto-compact status/threshold

4. **Effort persisted on runtime change** — `set_effort()` now stores to `_agent_options` for resume

## What we're watching

The Agent SDK is very new (first release ~March 2026, daily releases). Known areas of concern:

1. **Auto-compaction reliability** — The CLI compacts (we verified `compact_boundary` events in JSONL files), but we haven't confirmed the SDK reliably relays these as SystemMessage events to our Python consumer. Needs testing with a long session.

2. **Context reporting accuracy** — The context ring uses `usage.input_tokens + cache_read + cache_create` from AssistantMessage. This should represent per-request context, not cumulative. Need to verify against `get_context_usage()` data.

3. **SDK maturity** — Critical bugs are still being fixed (deadlocks, flag parsing, prompt handling). Expect to upgrade frequently.

## Why NOT raw API

The original plan proposed replacing the SDK with direct `anthropic.AsyncAnthropic()` calls to get explicit `context_management` control. We decided against this because:

- **Tool implementation burden**: Would need to implement Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Agent, and all future tools ourselves
- **System prompt maintenance**: The CLI's system prompt is complex and evolving — we'd have to maintain our own copy
- **Feature parity gap**: Skills, slash commands, CLAUDE.md loading, MCP servers, subagents, file checkpointing — all handled by the SDK for free
- **Anthropic's commitment**: The Agent SDK is explicitly intended to provide Claude Code-level capability. Fighting the SDK means fighting the platform direction.

## Fallback plan

If the SDK proves fundamentally broken for our use case (e.g., compaction never works reliably through the subprocess wrapper), the raw API approach remains documented in git history. The key pieces would be:
- `be_conductor/sessions/auth.py` — OAuth token extraction from `~/.claude/.credentials.json`
- `be_conductor/sessions/tools.py` — tool definitions (JSON schemas) + executors
- Direct `anthropic.AsyncAnthropic()` with `context_management` parameter

But this is a last resort. The Agent SDK should be the answer.
