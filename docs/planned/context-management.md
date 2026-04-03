# Context Management for Agent Sessions

## Problem

The Claude Agent SDK does not auto-compact or manage context size. The CLI does this internally but the SDK exposes none of it. This causes:

- Context blowing up to 7M+ tokens in a single turn (agent runs many tool calls)
- Auto-compact only fires between turns — useless if the agent burns through the limit in one turn
- Sessions become unrecoverable when context exceeds API limits
- Users lose their work and have to start over

## Solution: Anthropic API Context Management (January 2026+)

Three server-side strategies, all handled by the API — no client-side compression needed.

### 1. Server-Side Compaction (primary)

The API summarizes the conversation when input tokens exceed a threshold. After compaction, all messages before the summary are auto-dropped on the next request.

- Beta: `compact-2026-01-12`
- Trigger: `input_tokens` threshold (e.g., 150K)
- `pause_after_compaction`: lets us hook into the lifecycle (save state, track budget, push events to dashboard)
- Customizable compaction prompt

### 2. Tool Result Clearing (complementary)

Clears old tool results when context grows — keeps the tool call record but replaces the result with a placeholder. Ideal for agentic workflows with heavy file reads and shell output.

- Beta: `context-management-2025-06-27`
- `clear_tool_uses_20250919` with `keep: 6` (keep 6 most recent tool results)
- Can exclude specific tools (e.g., never clear memory reads)
- Reports how many tokens were freed

### 3. Memory Tool (cross-session persistence)

Server-side tool that Claude can use to save/read files in a memory directory. Pairs with compaction — important state survives compaction boundaries.

- Beta: `compact-2026-01-12`
- `memory_20250801` tool type
- Memory directory per session: `/path/to/project/.be-conductor/memories/{session}/`

## Implementation Plan

### Phase 1: Replace SDK agent loop with raw API loop

The SDK wraps the API but hides context management features. Replace `ClaudeSDKClient` with direct `anthropic.Anthropic()` calls using `context_management` parameter.

```python
response = client.beta.messages.create(
    betas=["compact-2026-01-12", "context-management-2025-06-27"],
    model="claude-opus-4-6",
    max_tokens=16384,
    messages=messages,
    context_management={
        "edits": [
            {"type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": 80000}, "keep": 6},
            {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 150000}, "pause_after_compaction": True},
        ]
    },
)
```

Agent loop handles `stop_reason`:
- `end_turn` → show response in GUI
- `tool_use` → execute tools, append results, continue loop
- `compaction` → save state, push event to dashboard, continue

### Phase 2: Dashboard integration

- Context usage indicator updates from `response.usage.input_tokens`
- Compaction events logged in timeline
- Token budget tracker per session
- Tool clearing stats shown when old results are dropped

### Phase 3: Memory integration

- Per-session memory directory
- Claude reads memory on session start
- Important state persists across compaction boundaries
- Replaces resume_id approach for GUI sessions

### Phase 4: Per-session configuration

```yaml
# ~/.be-conductor/sessions/{name}/agent_config.yaml
model: claude-opus-4-6
context_management:
  tool_clearing_trigger: 80000
  tool_clearing_keep: 6
  compaction_trigger: 150000
  pause_after_compaction: true
  token_budget: 3000000
memory_directory: .be-conductor/memories/{session}/
```

## What this replaces

- SDK's lack of context management
- Our hacky auto-compact between turns
- The interrupt-on-80% approach
- Silent context blowups

## Priority

Critical — this is the #1 usability gap between CLI and GUI agent sessions.
