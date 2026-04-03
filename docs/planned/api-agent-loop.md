# Replace SDK Agent Loop with Raw API + Context Management

## Task

Replace the `ClaudeSDKClient` agent loop in `be_conductor/sessions/agent_session.py` with a direct Anthropic API loop that uses the `context_management` betas. This gives us server-side compaction, tool result clearing, and memory — solving the context blowup problem.

## Why

The SDK wraps the API but hides context management features. It doesn't auto-compact, doesn't truncate tool outputs, and lets context blow up to 7M+ tokens in a single turn. The CLI handles this internally but the SDK doesn't expose it. Users lose entire sessions when context overflows.

## Current Architecture

File: `be_conductor/sessions/agent_session.py`

```
User prompt → _input_queue → client.query(text) → _stream_response(client)
                                                        │
                                                        ├── AssistantMessage → _emit_event (broadcast + history)
                                                        ├── ResultMessage → _emit_event (turn done)
                                                        └── RateLimitEvent → _broadcast_event
```

Key components:
- `_run()` method (line ~370): main agent loop, creates `ClaudeSDKClient`, processes prompts
- `_stream_response()` (line ~480): iterates `response_iter`, handles message types
- `_format_assistant()` (line ~570): converts SDK messages to our wire format
- `_can_use_tool()` (line ~274): permission callback
- `_ask_user_hook()` / `_exit_plan_hook()` (line ~182/210): PreToolUse hooks
- `answer_question()` (line ~910): receives answers from UI
- `_question_answer_queue`: asyncio.Queue for UI → agent answers

## Target Architecture

```
User prompt → _input_queue → _api_call(messages) → handle response
                                                        │
                                                        ├── stop_reason="end_turn" → emit assistant message, done
                                                        ├── stop_reason="tool_use" → execute tools, append results, loop
                                                        └── stop_reason="compaction" → emit compaction event, continue
```

### Core change: `_run()` method

Replace:
```python
async with ClaudeSDKClient(options=options) as client:
    await client.query(text)
    await self._stream_response(client)
```

With:
```python
import anthropic
client = anthropic.AsyncAnthropic()  # uses ANTHROPIC_API_KEY or OAuth

messages = []
system_prompt = self._build_system_prompt()
tools = self._get_tool_definitions()

# On resume: messages start empty, API picks up from conversation ID
# On new session: messages start with user prompt

while True:
    response = await client.beta.messages.create(
        betas=["compact-2026-01-12", "context-management-2025-06-27"],
        model=self._agent_options.get("model", "claude-opus-4-6"),
        max_tokens=16384,
        system=system_prompt,
        messages=messages,
        tools=tools,
        context_management={
            "edits": [
                {
                    "type": "clear_tool_uses_20250919",
                    "trigger": {"type": "input_tokens", "value": 80_000},
                    "keep": 6,
                },
                {
                    "type": "compact_20260112",
                    "trigger": {"type": "input_tokens", "value": 150_000},
                    "pause_after_compaction": True,
                },
            ]
        },
    )
    
    # Emit to UI
    self._emit_assistant_message(response)
    
    # Update context stats
    if response.usage:
        self._emit_usage(response.usage)
    
    # Handle applied edits (tool clearing, compaction)
    if response.context_management and response.context_management.applied_edits:
        for edit in response.context_management.applied_edits:
            self._emit_context_event(edit)
    
    # Route by stop_reason
    if response.stop_reason == "end_turn":
        break  # Turn complete, wait for next user prompt
    
    elif response.stop_reason == "compaction":
        # Context was compacted — continue the loop
        # Messages array is auto-trimmed by the API
        self._broadcast_event({"type": "system", "subtype": "compaction"})
        continue
    
    elif response.stop_reason == "tool_use":
        # Execute each tool call
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await self._execute_tool(block)
                tool_results.append(result)
        
        # Append assistant response + tool results to messages
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
        continue
```

### Tool execution: `_execute_tool()`

This replaces the SDK's internal tool execution. We need to implement each tool ourselves OR use the SDK's tool implementations if they're exposed separately.

Check if the SDK exposes tool implementations:
```python
from claude_agent_sdk import tools  # does this exist?
```

If not, we need to implement:
- **Read**: read file, return content (truncate at 100K chars)
- **Write**: write file
- **Edit**: apply edit (old_string → new_string)
- **Bash**: run command, return stdout+stderr (truncate at 50K chars)
- **Glob**: find files by pattern
- **Grep**: search file contents
- **WebFetch**: fetch URL
- **WebSearch**: search web
- **TodoWrite**: update todo list
- **AskUserQuestion**: emit question event, wait on queue
- **Agent**: spawn sub-agent (recursive)

For tools that need permission, call `_can_use_tool()` before executing.

**Important**: The tool definitions must match what Claude expects. Get them from the SDK or from the API docs.

### Auth

The current SDK uses OAuth from `~/.claude/.credentials.json`. The raw Anthropic client needs either:
- `ANTHROPIC_API_KEY` env var
- OAuth token passed as bearer auth

Check how the SDK authenticates and replicate:
```python
# The SDK reads ~/.claude/.credentials.json for OAuth
# We may need to extract the token and pass it to the Anthropic client
```

### Permission handling

Replace `can_use_tool` callback with direct checks before tool execution:
```python
async def _execute_tool(self, block):
    tool_name = block.name
    tool_input = block.input
    
    # Check permissions
    mode = self._agent_options.get("permission_mode", "default")
    if mode != "bypassPermissions":
        if mode == "default" or (mode == "acceptEdits" and tool_name == "Bash"):
            # Ask user
            answer = await self._ask_permission(tool_name, tool_input)
            if answer == "denied":
                return {"type": "tool_result", "tool_use_id": block.id, "content": "Permission denied", "is_error": True}
    
    # Execute
    result = await self._run_tool(tool_name, tool_input)
    return {"type": "tool_result", "tool_use_id": block.id, "content": result}
```

### Hooks (AskUserQuestion, ExitPlanMode)

These become just tool handlers in `_execute_tool()`:
```python
if tool_name == "AskUserQuestion":
    # Emit question to UI, wait for answer
    answer = await self._ask_user_question(tool_input)
    return {"content": answer}

if tool_name == "ExitPlanMode":
    # Show plan, wait for approval
    approved = await self._plan_review(tool_input)
    if approved:
        return {"content": "Plan approved"}
    else:
        return {"content": "Plan rejected", "is_error": True}
```

### Resume

The SDK uses `resume_id` (conversation ID). With raw API:
- Option A: Keep using SDK resume IDs if the API supports them
- Option B: Store messages array to disk, reload on resume
- Option C: Use memory tool — Claude reads memory directory on startup

Check if `anthropic.beta.messages.create()` accepts a `conversation_id` or similar.

### Streaming

For live UI updates, use streaming:
```python
async with client.beta.messages.stream(
    betas=[...],
    model=...,
    messages=messages,
    tools=tools,
    context_management={...},
) as stream:
    async for event in stream:
        # Forward text deltas to UI in real-time
        if event.type == "content_block_delta":
            self._broadcast_event({"type": "stream_delta", "delta": event.delta})
    
    response = await stream.get_final_message()
```

## Files to modify

- `be_conductor/sessions/agent_session.py` — main changes (replace SDK loop with API loop)
- `be_conductor/sessions/tools.py` — new file: tool implementations
- `be_conductor/sessions/auth.py` — new file: OAuth token management
- `be_conductor/static/agent-view.html` — handle new event types (compaction, tool_clearing)

## Migration strategy

1. Keep the SDK path working (don't delete it)
2. Add a new `AgentSessionV2` class with the API-based loop
3. Config flag to choose: `agent_backend: "sdk" | "api"`
4. Default to "api" once stable
5. Remove SDK path after validation

## Testing

1. New session: create, send prompt, verify tools work
2. Permission modes: ask, acceptEdits, bypass — all should work
3. Context management: verify compaction fires at threshold
4. Tool clearing: verify old tool results get cleared
5. Resume: stop session, resume, verify context is intact
6. Large sessions: run 50+ turns, verify no context blowup
7. Image attachments: verify they work through the API path
8. /btw: verify side questions still work (separate query path)
9. Multi-client: verify events broadcast to all connected clients

## Dependencies

- `anthropic` Python package (already available, used by BTW)
- Beta access to `compact-2026-01-12` and `context-management-2025-06-27`
- OAuth token extraction from `~/.claude/.credentials.json`

## Estimated scope

- Core loop replacement: 1 session
- Tool implementations: 1 session  
- Permission/hooks migration: 1 session
- Testing and migration: 1 session
