# Context Management for Agent Sessions

## Status: Handled by the Agent SDK

**Decision (April 2026):** Context management is handled internally by the Claude Agent SDK (which wraps the Claude Code CLI). We do not implement our own compaction, tool clearing, or context management. We focus on visibility and correct configuration.

## How it works

The Agent SDK spawns the Claude Code CLI as a subprocess. The CLI manages context internally:

1. **Auto-compaction**: When context approaches the window limit, the CLI summarizes older history. It emits `SystemMessage(subtype="compact_boundary")` through the SDK.
2. **Tool result management**: The CLI handles truncation and deduplication of tool results internally (e.g., Read tool deduplicates unchanged re-reads as of CLI v2.1.89).
3. **Prompt caching**: Static prefix (system prompt, tool definitions, CLAUDE.md) is prompt-cached across turns, reducing cost on repeated content.

## What we control

### Configuration (via ClaudeAgentOptions)

```python
options = ClaudeAgentOptions(
    # Core
    cwd=self.cwd,
    setting_sources=["user", "project"],  # loads CLAUDE.md, skills, hooks
    
    # Permission handling — always "default" so our GUI callback intercepts all tools
    permission_mode="default",
    can_use_tool=_can_use_tool,
    
    # Optional overrides — only passed if explicitly set by user
    effort="high",                          # low/medium/high/max
    thinking={"type": "adaptive"},          # adaptive, enabled (with budget), disabled
    max_budget_usd=10.0,                    # session cost cap
    max_turns=50,                           # turn limit
)
```

We do NOT override effort, thinking, or budget by default. The CLI uses its own defaults, matching terminal Claude behavior.

### Visibility (what we show in the GUI)

1. **Context ring** — shows per-request context usage (input + cache tokens) as percentage of 1M window
2. **Cost in tooltip** — running session cost shown on hover
3. **Compact boundary** — "Context compacted — older messages summarized" divider in timeline
4. **Budget/turn limits** — clear amber warning when `error_max_budget_usd` or `error_max_turns` is hit
5. **Context breakdown popup** — via `get_context_usage()` API (SDK v0.1.52+), shows per-category token usage, auto-compact status and threshold
6. **Frontend auto-compact** — sends `/compact` when context exceeds 80% (fires between turns as a safety net)

### Result subtypes we handle

| Subtype | Meaning | UI behavior |
|---------|---------|-------------|
| `success` | Task completed normally | Show result |
| `error_max_budget_usd` | Budget cap exceeded | Amber warning, user can continue |
| `error_max_turns` | Turn limit reached | Amber warning, user can continue |
| `error_max_structured_output_retries` | Output validation failed | Error display |
| `error_during_execution` | API/runtime error | Error display |

## What fixed the token waste (April 2026)

### Root cause: Outdated CLI with critical bugs

We were on SDK v0.1.51 (CLI v2.1.85). Upgrading to v0.1.56 (CLI v2.1.92) fixed:

| Bug | Impact | Fixed in |
|-----|--------|----------|
| Auto-compact thrash loop | Infinite compact/refill cycle burning API calls | CLI 2.1.89 |
| Nested CLAUDE.md re-injection | Same instructions injected dozens of times, bloating context | CLI 2.1.89 |
| Prompt cache misses in long sessions | Tool schema bytes changed mid-session, breaking caching | CLI 2.1.89 |
| Read tool duplicate reads | Re-reading unchanged files at full token cost | CLI 2.1.89 |
| Hook output injected into context | Outputs >50K chars dumped into context instead of saved to disk | CLI 2.1.89 |
| Edit tool verbose anchors | Unnecessarily long old_string anchors wasting output tokens | CLI 2.1.91 |

### Root cause: Overriding CLI defaults

We were forcing `thinking: {type: "enabled", budget_tokens: 10000}` and `max_budget_usd: 10.0` on every session. This overrode whatever the CLI uses internally. Fix: only pass these if the user explicitly sets them.

## Token usage explained

The API sends the full conversation history on every request. In a session with N tool calls:

```
Request 1:  system_prompt + user_prompt = ~12K tokens
Request 2:  all of above + response + tool result = ~25K
Request 3:  all of above + next response + result = ~45K
...
Request N:  accumulated context = up to 200K+
```

**Cumulative billing** = sum of all request sizes. A 30-turn session with average 100K context = ~3M total input tokens. This is normal and expected — not a bug. Compaction reduces later request sizes by summarizing older history.

**Prompt caching** helps: the static prefix (system prompt, tool definitions) is cached and only paid once at full price. Subsequent requests read from cache at reduced cost.

## SDK maturity notes

The Claude Agent SDK is very new (first Python release ~March 2026). Current version is 0.1.56 with daily releases. Key considerations:

- **Keep upgrading** — critical fixes land frequently. Check `pip index versions claude-agent-sdk` regularly.
- **Verify compaction** — after long sessions, check if `compact_boundary` events actually appear in the GUI timeline. If not, the SDK may not be relaying them properly.
- **Use `get_context_usage()`** — the "Context breakdown" popup shows real data from the CLI, not our estimate. Use it to verify the context ring is accurate.
- **Subagents for isolation** — for complex multi-step tasks, subagents start with fresh context. The parent only sees the final summary, not the full subtask transcript.

## Previous approaches (superseded)

These were considered but are no longer the plan:

1. ~~**Raw API with `context_management` parameter**~~ — Would give explicit control over compaction/clearing, but requires implementing all tools ourselves. Not worth the maintenance burden.
2. ~~**Frontend auto-compact at 80%**~~ — Still exists as a safety net, but the CLI should handle compaction internally. May remove if SDK proves reliable.
3. ~~**Default thinking budget (10K)**~~ — Overriding CLI defaults caused more problems than it solved. Removed.
4. ~~**Default max_budget_usd ($10)**~~ — Same issue. Removed.
