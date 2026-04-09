"""Agent SDK session — structured events instead of raw PTY output.

Wraps the Claude Agent SDK (or future agent SDKs) and streams typed
JSON events to subscribers.  A parallel console buffer accumulates
ANSI-formatted text so get_buffer() / get_screen_snapshot() still
work for backwards-compatible consumers (CLI, console-mode toggle).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Set

log = logging.getLogger(__name__)

BUFFER_MAX_BYTES = 1_000_000

# Reuse the ANSI-stripping regex from session.py
_ANSI_RE = re.compile(
    r'\x1b'
    r'(?:'
    r'\[[\x20-\x3f]*[a-zA-Z@-~]'
    r'|\][^\x07]*\x07'
    r'|[()][AB012]'
    r'|[>=<]'
    r'|#[0-9]'
    r'|.'
    r')'
)


def _format_event_ansi(event: dict) -> str | None:
    """Format a structured agent event as ANSI-colored terminal text."""
    etype = event.get("type")

    if etype == "user_message":
        content = event.get("content", "")
        return f"\r\n\033[1;36m>>> User\033[0m\r\n{content}\r\n"

    elif etype == "assistant_message":
        parts = []
        for block in event.get("content", []):
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "thinking":
                parts.append("\033[90m[thinking...]\033[0m")
            elif btype == "tool_use":
                tool = block.get("tool", "")
                inp = block.get("input", {})
                summary = str(inp)[:200]
                parts.append(f"\033[1;33m[{tool}]\033[0m {summary}")
            elif btype == "tool_result":
                content = block.get("content", "")
                if block.get("is_error"):
                    parts.append(f"\033[1;31m[error]\033[0m {str(content)[:200]}")
                else:
                    parts.append(f"\033[1;32m[result]\033[0m {str(content)[:200]}")
        if parts:
            return "\r\n\033[1;32m<<< Claude\033[0m\r\n" + "\r\n".join(parts) + "\r\n"

    elif etype == "result":
        result = event.get("result", "")
        cost = event.get("total_cost_usd")
        suffix = f" (${cost:.4f})" if cost else ""
        return f"\r\n\033[1;35m[done]\033[0m {result}{suffix}\r\n"

    elif etype == "error":
        error = event.get("error", "")
        return f"\r\n\033[1;31m[error] {error}\033[0m\r\n"

    elif etype == "session_end":
        code = event.get("exit_code", 0)
        return f"\r\n[session ended (exit {code})]\r\n"

    return None


class AgentSession:
    """A session backed by the Claude Agent SDK (or future agent SDKs)."""

    session_type: str = "agent"

    def __init__(
        self,
        name: str,
        prompt: str,
        session_id: str | None = None,
        cwd: str | None = None,
        on_exit=None,
        env: dict | None = None,
        worktree: dict | None = None,
        notifier=None,
        agent_options: dict | None = None,
    ):
        self.id = session_id or name
        self.name = name
        self.command = "claude"
        self.prompt = prompt
        self.cwd = cwd
        self.worktree = worktree
        self.status = "starting"
        self.pid: int | None = None
        self.start_time: float | None = None
        self.created_at: str | None = None
        self.exit_code: int | None = None
        self.resume_id: str | None = None
        self.rows: int = 24
        self.cols: int = 80
        self.subscribers: Set[asyncio.Queue] = set()
        self._on_exit = on_exit
        self._notifier = notifier
        self._agent_options = agent_options or {}

        # Console buffer (ANSI text for get_buffer / console mode)
        self._console_buffer = bytearray()

        # Structured message history (for replay to new subscribers)
        self._message_history: list[dict] = []
        self._load_history()

        # SDK state
        self._client: Any = None
        self._run_task: asyncio.Task | None = None
        self._input_queue: asyncio.Queue[dict | str] = asyncio.Queue()
        self._processing = False  # True while agent is processing a turn
        self._pending_prompts: list[dict] = []  # server-side message queue
        self._question_pending = False  # True while a permission question is shown

        # Client tracking
        self._attached_sources: dict[str, str] = {}
        self.resize_source: str | None = None
        self.resize_owner_id: str | None = None
        self.browser_resize_owner_id: str | None = None
        self.cli_attach_count: int = 0

    async def start(self, rows: int = 24, cols: int = 80) -> None:
        self.rows = rows
        self.cols = cols
        self.start_time = time.time()
        self.created_at = datetime.fromtimestamp(
            self.start_time, tz=timezone.utc
        ).isoformat()
        self.status = "running"
        self._run_task = asyncio.create_task(self._agent_loop())

    async def _agent_loop(self) -> None:
        """Main loop: send prompt, stream responses, accept follow-ups."""
        try:
            from claude_agent_sdk import (
                ClaudeSDKClient,
                ClaudeAgentOptions,
                AssistantMessage,
                ResultMessage,
                SystemMessage,
                StreamEvent,
                RateLimitEvent,
            )
        except ImportError:
            self._emit_event({
                "type": "error",
                "error": (
                    "claude-agent-sdk is not installed. "
                    "Install with: pip install claude-agent-sdk"
                ),
            })
            self.status = "exited"
            self.exit_code = 1
            self._broadcast_close()
            if self._on_exit:
                await self._on_exit(self.id)
            return

        resume_id = self._agent_options.get("resume")
        if resume_id:
            self.resume_id = resume_id

        # Queue for receiving answers to AskUserQuestion from the UI
        self._question_answer_queue: asyncio.Queue[str] = asyncio.Queue()

        # Hook to intercept ExitPlanMode — show plan to user for approval.
        async def _exit_plan_hook(hook_input, match=None, context=None):
            from pathlib import Path
            tool_input = hook_input.get("tool_input", {})
            tool_use_id = hook_input.get("tool_use_id", "")

            # Read the plan file if specified
            plan_content = ""
            plan_file = tool_input.get("planFile", "")
            if not plan_file:
                import glob as _glob
                plan_files = _glob.glob(
                    str(Path.home() / ".claude/plans/*.md"))
                if plan_files:
                    latest = max(plan_files, key=lambda f: Path(f).stat().st_mtime)
                    try:
                        plan_content = Path(latest).read_text(encoding="utf-8")
                        plan_file = latest
                    except Exception:
                        pass
            elif plan_file:
                try:
                    plan_content = Path(plan_file).read_text(encoding="utf-8")
                except Exception:
                    pass

            # Emit plan review event to UI
            self._question_pending = True
            self._emit_event({
                "type": "plan_review",
                "plan": plan_content,
                "plan_file": plan_file,
                "tool_use_id": tool_use_id,
            })

            # Wait for user approval
            try:
                answer = await asyncio.wait_for(
                    self._question_answer_queue.get(), timeout=600
                )
            except asyncio.TimeoutError:
                answer = "rejected"

            # Exit plan mode → switch back to default
            self._current_mode = "default"
            self._agent_options["permission_mode"] = "default"
            self._pending_mode_change = "default"
            self._broadcast_settings()
            if answer.lower() in ("approve", "approved", "yes", "ok"):
                return {}  # Allow the tool to proceed (no decision = continue)
            else:
                return {"decision": "block", "reason": answer}

        # SDK permission callback — the SDK decides WHEN to ask (based on
        # permission_mode), this callback decides HOW to show the prompt.
        from claude_agent_sdk import (
            PermissionResultAllow, PermissionResultDeny,
        )

        # Hook for AskUserQuestion — parse structured questions, show them,
        # collect answers, return allow with updated_input containing answers.
        async def _ask_user_hook(hook_input, tool_use_id=None, context=None):
            tool_input = hook_input.get("tool_input", {})
            questions = tool_input.get("questions", [])
            answers = {}
            for q in questions:
                q_text = q.get("question", "")
                if not self._question_pending:
                    while not self._question_answer_queue.empty():
                        try: self._question_answer_queue.get_nowait()
                        except: break
                self._question_pending = True
                self._emit_event({
                    "type": "question",
                    "source": "ask_user",
                    "question": q_text,
                    "header": q.get("header", ""),
                    "options": q.get("options", []),
                    "multiSelect": q.get("multiSelect", False),
                })
                try:
                    answer = await asyncio.wait_for(
                        self._question_answer_queue.get(), timeout=300
                    )
                except asyncio.TimeoutError:
                    answer = ""
                    self._question_pending = False
                # question_answered is emitted by answer_question() which
                # also handles multi-client dismissal — don't duplicate here.
                answers[q_text] = answer
            return {
                "continue_": True,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {
                        "questions": questions,
                        "answers": answers,
                    },
                },
            }

        async def _can_use_tool(tool_name, tool_input, context):
            # AskUserQuestion is fully handled by its PreToolUse hook.
            if tool_name == "AskUserQuestion":
                return PermissionResultAllow()
            # Permission mode shortcuts
            mode = self._agent_options.get("permission_mode", "default")
            if mode == "plan":
                # Plan mode: block all tools except read-only exploration
                # and AskUserQuestion (handled above). Claude plans but doesn't act.
                if tool_name in ("Read", "Glob", "Grep", "ToolSearch",
                                 "EnterPlanMode", "ExitPlanMode", "Agent",
                                 "TodoWrite"):
                    return PermissionResultAllow()
                return PermissionResultDeny(
                    message="Plan mode is active — tools are blocked until the plan is approved."
                )
            if mode == "bypassPermissions":
                return PermissionResultAllow()
            if mode == "acceptEdits" and tool_name in ("Edit", "Write", "NotebookEdit"):
                return PermissionResultAllow()
            # Build a readable summary for the permission prompt
            if tool_name == "Bash":
                prompt = tool_input.get("command", "") or tool_input.get("description", "")
            elif tool_name in ("Edit", "Write", "NotebookEdit", "Read"):
                prompt = tool_input.get("file_path", "")
            elif tool_name == "Glob":
                prompt = tool_input.get("pattern", "")
            elif tool_name == "Grep":
                prompt = tool_input.get("pattern", "")
            elif tool_name == "Skill":
                skill = tool_input.get("skill", "")
                args = tool_input.get("args", "")
                prompt = f"{skill} {args}".strip() if args else skill
            elif tool_name == "Agent":
                prompt = tool_input.get("description", "") or tool_input.get("prompt", "")[:100]
            elif tool_name == "WebFetch":
                prompt = tool_input.get("url", "")
            elif tool_name == "WebSearch":
                prompt = tool_input.get("query", "")
            else:
                # Generic: pick the first string-valued field
                prompt = ""
                for v in tool_input.values():
                    if isinstance(v, str) and v:
                        prompt = v[:200]
                        break
                if not prompt:
                    prompt = str(tool_input)[:200]

            # Only drain if no question is pending (avoids losing a fresh answer)
            if not self._question_pending:
                while not self._question_answer_queue.empty():
                    try: self._question_answer_queue.get_nowait()
                    except: break
            self._question_pending = True

            self._emit_event({
                "type": "question",
                "question": f"Allow **{tool_name}**: `{prompt}`?",
                "options": [
                    {"label": "Yes", "value": "yes"},
                    {"label": "Yes, allow all this session", "value": "yes_all"},
                    {"label": "No", "value": "no"},
                ],
            })

            try:
                answer = await asyncio.wait_for(
                    self._question_answer_queue.get(), timeout=300
                )
            except asyncio.TimeoutError:
                return PermissionResultDeny(message="No answer (timeout)")

            self._emit_event({
                "type": "system",
                "subtype": "debug",
                "data": {"permission_answer": answer},
            })

            if answer.lower() in ("yes", "yes_all", "approve", "approved", "ok"):
                if answer.lower() == "yes_all":
                    # Mark bypass internally — can't call set_permission_mode here
                    # because we're inside can_use_tool callback (would deadlock).
                    # The mode change is applied after this callback returns.
                    self._agent_options["permission_mode"] = "bypassPermissions"
                    self._pending_mode_change = "bypassPermissions"
                return PermissionResultAllow()
            if answer.lower() in ("no", "deny", "denied", "cancel", "cancelled"):
                return PermissionResultDeny(message="User denied this action.")
            # Free text — deny with the user's instructions so the agent
            # can see what they want changed and act on it.
            return PermissionResultDeny(message=answer)

        # Catch-all hook: keeps stream open while can_use_tool waits for
        # the user to click Allow/Deny.  Without this the SDK may close
        # the stream before the permission callback completes.
        # Returns continue_=True but NO permissionDecision — so the SDK
        # falls through to can_use_tool for the actual decision.
        # AskUserQuestion and ExitPlanMode are excluded (they have their
        # own hooks that return permissionDecision="allow").
        async def _continue_hook(hook_input, match=None, context=None):
            return {"continue_": True}

        # PreCompact hook: emits event to UI when compaction is about to
        # happen — more reliable than waiting for compact_boundary.
        async def _pre_compact_hook(hook_input, match=None, context=None):
            trigger = hook_input.get("trigger", "auto")
            self._broadcast_event({
                "type": "system",
                "subtype": "pre_compact",
                "data": {"trigger": trigger},
            })
            return {}

        # Stop hook: notifies UI when agent execution ends.
        async def _stop_hook(hook_input, match=None, context=None):
            return {}

        try:
            from claude_agent_sdk.types import HookMatcher
            hooks_config = {
                "PreToolUse": [
                    HookMatcher(matcher="AskUserQuestion", hooks=[_ask_user_hook]),
                    HookMatcher(matcher="ExitPlanMode", hooks=[_exit_plan_hook]),
                    HookMatcher(matcher="^(?!AskUserQuestion$|ExitPlanMode$)", hooks=[_continue_hook]),
                ],
                "PreCompact": [HookMatcher(matcher=None, hooks=[_pre_compact_hook])],
                "Stop": [HookMatcher(matcher=None, hooks=[_stop_hook])],
            }
        except Exception:
            hooks_config = None

        # Always use "default" permission_mode in the SDK so it calls our
        # can_use_tool callback for EVERY tool.  We handle the actual mode
        # logic (acceptEdits, bypassPermissions, plan) ourselves in the
        # callback — the SDK's built-in modes skip the callback for tools
        # it considers "safe" and falls back to interactive CLI prompts
        # that don't work in the GUI.
        # All other options (effort, thinking, max_budget_usd) are left at
        # CLI defaults unless explicitly overridden via agent_options.
        opts_kwargs: dict = {
            "cwd": self.cwd or ".",
            "allowed_tools": self._agent_options.get("allowed_tools"),
            "permission_mode": "default",
            "can_use_tool": _can_use_tool,
            "system_prompt": self._agent_options.get("system_prompt"),
            "max_turns": self._agent_options.get("max_turns"),
            "model": self._agent_options.get("model"),
            # Use resume (specific session ID) only — never continue_conversation
            # which picks "most recent session in cwd" and can cross-contaminate.
            # For new sessions, pass a fresh session_id to prevent the SDK from
            # silently picking up a recent session from the same cwd.
            "resume": resume_id,
            "continue_conversation": False,
            "include_partial_messages": True,
            "setting_sources": ["user", "project"],
        }
        # Force a fresh session_id for new sessions to prevent the SDK from
        # picking up a recent session from the same cwd.
        if not resume_id:
            import uuid as _uuid
            fresh_sid = str(_uuid.uuid4())
            opts_kwargs["session_id"] = fresh_sid
            log.info("New agent session %s: forcing session_id=%s", self.id, fresh_sid)
        # Only pass these if explicitly set — let the CLI use its own defaults
        for key in ("effort", "thinking", "max_budget_usd"):
            val = self._agent_options.get(key)
            if val is not None:
                opts_kwargs[key] = val
        options = ClaudeAgentOptions(**opts_kwargs)
        # Try to add hooks (SDK version may not support them)
        if hooks_config:
            try:
                options.hooks = hooks_config
            except Exception:
                pass

        try:
            async with ClaudeSDKClient(options=options) as client:
                self._client = client

                # Send initial prompt (skip if empty or just a command name)
                initial = self.prompt.strip()
                if initial and initial not in ("claude", "claude-agent", "Resume session"):
                    # Assign a turn_id so all events from this turn are tagged
                    if not hasattr(self, '_turn_prefix'):
                        import uuid as _uuid
                        self._turn_prefix = _uuid.uuid4().hex[:6]
                    self._turn_counter = getattr(self, '_turn_counter', 0) + 1
                    initial_turn_id = f"turn-{self._turn_prefix}-{self._turn_counter}"
                    self._current_turn_id = initial_turn_id
                    self._current_turn_btw = False
                    self._emit_event({
                        "type": "user_message",
                        "content": initial,
                        "turn_id": initial_turn_id,
                    })
                    self._processing = True
                    await client.query(initial)
                    await self._stream_response(client)
                    self._processing = False

                # Process any messages that were queued during initial prompt
                while self._pending_prompts and self.status == "running":
                    await self._process_pending(client)

                # Wait for follow-up prompts
                while self.status == "running":
                    try:
                        item = await self._input_queue.get()
                        if isinstance(item, dict) and item.get("_shutdown"):
                            break
                        is_btw = False
                        if isinstance(item, dict):
                            text = item.get("text", "")
                            attachments = item.get("attachments")
                            is_btw = item.get("_btw", False)
                        else:
                            text = item
                            attachments = None
                        # Assign a turn ID to group query + response
                        # Use a unique prefix per agent loop to avoid collisions after stop/resume
                        if not hasattr(self, '_turn_prefix'):
                            import uuid as _uuid
                            self._turn_prefix = _uuid.uuid4().hex[:6]
                        self._turn_counter = getattr(self, '_turn_counter', 0) + 1
                        turn_id = f"turn-{self._turn_prefix}-{self._turn_counter}"
                        self._current_turn_id = turn_id
                        self._current_turn_btw = is_btw
                        if is_btw:
                            self._broadcast_event({"type": "btw_start", "text": text})
                        # Emit to all clients + save to history (skip for btw)
                        if not is_btw:
                            self._emit_event({
                                "type": "user_message",
                                "content": text,
                                "turn_id": turn_id,
                            })
                        self._processing = True
                        if attachments:
                            prompt_with_files = self._build_prompt_with_attachments(
                                text, attachments
                            )
                            await client.query(prompt_with_files)
                        else:
                            await client.query(text)
                        await self._stream_response(client, is_btw=is_btw)
                        self._processing = False
                        # Notify clients the turn is complete — ensures
                        # the frontend removes the spinner even if the
                        # result event was missed or delayed.
                        self._broadcast_event({"type": "system", "subtype": "turn_complete"})
                        # Apply deferred mode change (from yes_all inside can_use_tool).
                        # Never send bypassPermissions to the SDK — it disables hooks
                        # (including our AskUserQuestion hook).  We handle bypass
                        # ourselves in _can_use_tool; just broadcast the UI update.
                        if getattr(self, '_pending_mode_change', None):
                            mode = self._pending_mode_change
                            self._pending_mode_change = None
                            if mode != "bypassPermissions":
                                try:
                                    await client.set_permission_mode(mode)
                                except Exception:
                                    pass
                            self._broadcast_settings()
                        if is_btw:
                            self._broadcast_event({"type": "btw_end"})

                        # Process any messages queued during this turn
                        while self._pending_prompts and self.status == "running":
                            await self._process_pending(client)
                    except asyncio.CancelledError:
                        # Interrupt — stay in the loop, wait for next prompt
                        continue
                    except Exception as turn_err:
                        # Per-turn error — don't kill the session
                        err_msg = str(turn_err)
                        # Ignore known interrupt-related errors
                        if "interrupt" in err_msg.lower() or "cancel" in err_msg.lower():
                            continue
                        self._emit_event({
                            "type": "error",
                            "error": err_msg,
                        })
                        # Stay in the loop — user can retry

        except Exception as e:
            # Fatal error (SDK connection failed, etc.)
            err_msg = str(e)
            if err_msg and "interrupt" not in err_msg.lower():
                self._emit_event({"type": "error", "error": err_msg})
        finally:
            self._client = None
            if self.status not in ("exited", "killed"):
                self.status = "exited"
                self.exit_code = 0
            self._emit_event({
                "type": "session_end",
                "exit_code": self.exit_code,
            })
            self._broadcast_close()
            if self._on_exit:
                await self._on_exit(self.id)

    async def _stream_response(self, client: Any, is_btw: bool = False) -> None:
        """Stream all messages from one query() call.

        is_btw: if True, tag all emitted events with btw=true so the
        frontend routes responses to the btw panel, not the main chat.
        This is captured here at call time — NOT read from shared state —
        to prevent races when a new turn starts before btw streaming finishes.
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            SystemMessage,
            StreamEvent,
            RateLimitEvent,
        )

        # Local emit that unconditionally tags with is_btw captured at call time.
        # This prevents races with shared _current_turn_btw state.
        def _emit(ev: dict) -> None:
            if is_btw:
                ev['btw'] = True
            self._emit_event(ev)
        def _bcast(ev: dict) -> None:
            if is_btw:
                ev['btw'] = True
            self._broadcast_event(ev)

        try:
            response_iter = client.receive_response()
        except Exception as e:
            _emit({
                "type": "error",
                "error": f"Response stream failed: {e}",
            })
            return

        async for message in response_iter:
            if isinstance(message, AssistantMessage):
                formatted = self._format_assistant(message)
                _emit(formatted)
            elif isinstance(message, ResultMessage):
                subtype = getattr(message, "subtype", None)
                _emit({
                    "type": "result",
                    "result": message.result,
                    "subtype": subtype,
                    "is_error": message.is_error,
                    "stop_reason": getattr(message, "stop_reason", None),
                    "duration_ms": message.duration_ms,
                    "num_turns": message.num_turns,
                    "total_cost_usd": getattr(message, "total_cost_usd", None),
                    "usage": getattr(message, "usage", None),
                    "model_usage": getattr(message, "model_usage", None),
                    "session_id": message.session_id,
                })
                self.resume_id = message.session_id
                # Persist metadata now that we have a resume ID —
                # if the server crashes, this survives for recovery.
                if self._on_exit:
                    try:
                        from be_conductor.utils.config import SESSIONS_DIR
                        import json as _json
                        path = SESSIONS_DIR / f"{self.id}.json"
                        path.write_text(_json.dumps(self.to_dict(), indent=2))
                    except Exception:
                        pass
            elif isinstance(message, SystemMessage):
                _emit({
                    "type": "system",
                    "subtype": message.subtype,
                    "data": message.data,
                })
                # Sync permission mode from SDK → our state → all clients.
                # DON'T overwrite bypassPermissions — that's a user choice
                # (via yes_all) that the SDK doesn't know about and would
                # reset on next system message.
                pm = message.data.get("permissionMode")
                current_pm = self._agent_options.get("permission_mode")
                if pm and pm != current_pm and current_pm != "bypassPermissions":
                    self._agent_options["permission_mode"] = pm
                    self._current_mode = pm
                    self._broadcast_settings()
                if message.subtype == "init":
                    sid = message.data.get("session_id")
                    if sid:
                        if self.resume_id and sid != self.resume_id:
                            log.warning(
                                "SDK session ID changed for %s: %s → %s "
                                "(possible compaction/fork — context may have shifted)",
                                self.id, self.resume_id, sid,
                            )
                            _bcast({
                                "type": "error",
                                "error": (
                                    "Context shift detected — the SDK switched "
                                    "to a different conversation after compaction. "
                                    "Responses may not match this session's history."
                                ),
                            })
                        self.resume_id = sid
                        # Persist IMMEDIATELY — belt and suspenders for crash recovery
                        try:
                            from be_conductor.utils.config import SESSIONS_DIR
                            import json as _json
                            path = SESSIONS_DIR / f"{self.id}.json"
                            path.write_text(_json.dumps(self.to_dict(), indent=2))
                        except Exception:
                            pass
            elif isinstance(message, StreamEvent):
                # Real-time text/thinking deltas for live UI rendering.
                # Ephemeral — not saved to history (AssistantMessage has final content).
                event = getattr(message, "event", {})
                etype = event.get("type", "")
                if etype == "content_block_delta":
                    delta = event.get("delta", {})
                    dtype = delta.get("type", "")
                    if dtype == "text_delta":
                        _bcast({
                            "type": "stream_delta",
                            "delta_type": "text",
                            "text": delta.get("text", ""),
                        })
                    elif dtype == "thinking_delta":
                        _bcast({
                            "type": "stream_delta",
                            "delta_type": "thinking",
                            "thinking": delta.get("thinking", ""),
                        })
                elif etype == "content_block_start":
                    cb = event.get("content_block", {})
                    _bcast({
                        "type": "stream_start",
                        "block_type": cb.get("type", ""),
                        "index": event.get("index", 0),
                    })
                elif etype == "content_block_stop":
                    _bcast({
                        "type": "stream_stop",
                        "index": event.get("index", 0),
                    })
            elif isinstance(message, RateLimitEvent):
                rli = getattr(message, "rate_limit_info", None)
                if rli:
                    _bcast({
                        "type": "rate_limit",
                        "status": getattr(rli, "status", None),
                        "utilization": getattr(rli, "utilization", None),
                        "resets_at": getattr(rli, "resets_at", None),
                        "rate_limit_type": getattr(rli, "rate_limit_type", None),
                        "overage_status": getattr(rli, "overage_status", None),
                        "raw": getattr(rli, "raw", None),
                    })

    def _emit_plan_review(self) -> None:
        """Read the latest plan file and emit a plan_review event."""
        from pathlib import Path
        import glob as _glob
        plan_content = ""
        plan_file = ""
        plan_files = _glob.glob(
            str(Path.home() / ".claude/plans/*.md"))
        if plan_files:
            latest = max(plan_files, key=lambda f: Path(f).stat().st_mtime)
            try:
                plan_content = Path(latest).read_text(encoding="utf-8")
                plan_file = latest
            except Exception:
                pass
        self._emit_event({
            "type": "plan_review",
            "plan": plan_content,
            "plan_file": plan_file,
        })

    @staticmethod
    def _format_assistant(message: Any) -> dict:
        """Convert an AssistantMessage to our wire format."""
        from claude_agent_sdk import (
            TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
        )

        blocks: list[dict] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ThinkingBlock):
                blocks.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                })
            elif isinstance(block, ToolUseBlock):
                blocks.append({
                    "type": "tool_use",
                    "tool": block.name,
                    "tool_use_id": block.id,
                    "input": block.input,
                })
            elif isinstance(block, ToolResultBlock):
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": (
                        str(block.content) if block.content else None
                    ),
                    "is_error": getattr(block, "is_error", False),
                })

        return {
            "type": "assistant_message",
            "content": blocks,
            "model": getattr(message, "model", None),
            "usage": getattr(message, "usage", None),
        }

    # ------------------------------------------------------------------
    # Event broadcast
    # ------------------------------------------------------------------

    @staticmethod
    def _json_safe(obj: Any) -> Any:
        """Recursively convert non-serializable objects to strings."""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, dict):
            return {k: AgentSession._json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [AgentSession._json_safe(v) for v in obj]
        return str(obj)

    def _history_path(self):
        """Path to the persisted message history file."""
        from be_conductor.utils.config import SESSIONS_DIR
        return SESSIONS_DIR / f"{self.id}.history.json"

    def _load_history(self) -> None:
        """Load message history from disk (if exists)."""
        import json as _json
        path = self._history_path()
        if path.exists():
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    # Filter out stale session_end events from previous runs
                    data = [e for e in data if e.get("type") not in ("session_end",)]
                    self._message_history = data
                    for event in data:
                        self._append_console(event)
            except Exception:
                pass

    def _save_history(self) -> None:
        """Persist message history to disk."""
        import json as _json
        try:
            path = self._history_path()
            path.write_text(
                _json.dumps(self._message_history, ensure_ascii=False,
                            default=str),
                encoding="utf-8",
            )
        except Exception as e:
            import sys
            print(f"[be-conductor] _save_history failed for {self.id}: {e}",
                  file=sys.stderr)

    def delete_history(self) -> None:
        """Remove persisted history file."""
        try:
            self._history_path().unlink(missing_ok=True)
        except Exception:
            pass

    def _save_to_history(self, event: dict) -> None:
        """Save event to history + disk only (no broadcast, no console)."""
        event = self._json_safe(event)
        event.setdefault("timestamp", time.time())
        self._message_history.append(event)
        self._save_history()

    def _broadcast_event(self, event: dict) -> None:
        """Broadcast event to subscribers WITHOUT saving to history."""
        event = self._json_safe(event)
        event.setdefault("timestamp", time.time())
        # Tag with current turn_id (same logic as _emit_event) so the
        # frontend can route stream_delta/btw/etc to the correct group.
        turn_id = getattr(self, '_current_turn_id', None)
        if turn_id and 'turn_id' not in event:
            event['turn_id'] = turn_id
            if getattr(self, '_current_turn_btw', False):
                event['btw'] = True
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("Dropped broadcast event (queue full): %s", event.get("type"))

    def _emit_event(self, event: dict) -> None:
        """Broadcast a structured event and append to console buffer."""
        event = self._json_safe(event)
        event.setdefault("timestamp", time.time())
        # Tag with current turn ID for query/response grouping
        turn_id = getattr(self, '_current_turn_id', None)
        if turn_id and 'turn_id' not in event:
            event['turn_id'] = turn_id
            if getattr(self, '_current_turn_btw', False):
                event['btw'] = True
        # BTW events are ephemeral — broadcast only, don't save to history
        if not event.get('btw'):
            self._message_history.append(event)
            self._save_history()
        self._append_console(event)

        # Fire notification for events that need user attention
        etype = event.get("type")
        if etype in ("question", "error", "plan_review") and self._notifier:
            import asyncio
            from be_conductor.notifications.manager import NotificationEvent
            reason = {
                "question": "Needs your answer",
                "error": "Error occurred",
                "plan_review": "Plan ready for review",
            }.get(etype, "Needs attention")
            snippet = event.get("question", event.get("error", event.get("plan", "")))
            if isinstance(snippet, str):
                snippet = snippet[:120]
            else:
                snippet = str(snippet)[:120]
            notif = NotificationEvent(
                session_id=self.id,
                session_name=self.name,
                reason=reason,
                snippet=snippet,
            )
            try:
                asyncio.ensure_future(self._notifier._manager.notify(notif))
            except Exception:
                pass

        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Coalesce: drain and re-enqueue recent items
                merged: list[dict] = []
                try:
                    while not queue.empty():
                        merged.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    pass
                merged.append(event)
                for item in merged[-100:]:
                    try:
                        queue.put_nowait(item)
                    except asyncio.QueueFull:
                        break

    def _append_console(self, event: dict) -> None:
        text = _format_event_ansi(event)
        if text:
            data = text.encode("utf-8", errors="replace")
            self._console_buffer.extend(data)
            if len(self._console_buffer) > BUFFER_MAX_BYTES:
                excess = len(self._console_buffer) - BUFFER_MAX_BYTES
                del self._console_buffer[:excess]

    async def _process_pending(self, client: Any) -> None:
        """Pop the next queued message and process it as a normal turn."""
        if not self._pending_prompts:
            return
        entry = self._pending_prompts.pop(0)
        text = entry.get("text", "")
        attachments = entry.get("attachments")

        if not hasattr(self, '_turn_prefix'):
            import uuid as _uuid
            self._turn_prefix = _uuid.uuid4().hex[:6]
        self._turn_counter = getattr(self, '_turn_counter', 0) + 1
        turn_id = f"turn-{self._turn_prefix}-{self._turn_counter}"
        self._current_turn_id = turn_id
        self._current_turn_btw = False

        # Emit promoted event so clients update the queued message visual
        self._emit_event({
            "type": "queued_promoted",
            "content": text,
            "turn_id": turn_id,
        })

        self._processing = True
        if attachments:
            prompt_with_files = self._build_prompt_with_attachments(
                text, attachments
            )
            await client.query(prompt_with_files)
        else:
            await client.query(text)
        await self._stream_response(client)
        self._processing = False

    def _broadcast_close(self) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    # Public interface (matches SessionProtocol)
    # ------------------------------------------------------------------

    def send_input(
        self,
        text: str,
        attachments: list[dict] | None = None,
        btw: bool = False,
    ) -> None:
        """Enqueue a follow-up prompt, optionally with file attachments.

        If the agent is currently processing a turn, the message is
        queued server-side and a ``queued_message`` event is emitted so
        all connected clients can display it.  When the current turn
        finishes, the queued message is automatically promoted and sent
        to the agent.
        """
        msg: dict | str
        if btw:
            # BTW: if agent is busy, use direct Anthropic API (concurrent).
            # If idle, go through normal queue.
            if self._processing:
                import asyncio
                asyncio.create_task(self._send_btw(text))
            else:
                self._input_queue.put_nowait({"text": text, "_btw": True})
            return
        if attachments:
            msg = {"text": text, "attachments": attachments}
            self._input_queue.put_nowait(msg)
            return
        else:
            msg = text

        # If agent is busy, queue server-side instead of sending to _input_queue
        if self._processing:
            entry = {"text": text}
            if attachments:
                entry["attachments"] = attachments
            self._pending_prompts.append(entry)
            self._emit_event({
                "type": "queued_message",
                "content": text,
                "queue_index": len(self._pending_prompts) - 1,
            })
            return

        self._input_queue.put_nowait(msg)

    async def _send_btw(self, text: str) -> None:
        """Answer a /btw question using SDK query() — parallel, no API key needed.

        Uses the same OAuth auth as the main agent. Runs as an independent
        one-shot conversation with context from recent history.
        Fully ephemeral — nothing saved to disk.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

        self._broadcast_event({"type": "btw_start", "text": text})

        try:
            # Build context from recent non-btw history
            context_lines = []
            for ev in self._message_history[-30:]:
                if ev.get("btw"):
                    continue
                if ev.get("type") == "user_message":
                    context_lines.append("User: " + (ev.get("content", ""))[:300])
                elif ev.get("type") == "assistant_message":
                    for b in ev.get("content", []):
                        if b.get("type") == "text":
                            context_lines.append("Assistant: " + b.get("text", "")[:300])
                            break
            context = "\n\n".join(context_lines[-20:])

            prompt = (
                "You have the following conversation context:\n\n"
                f"{context}\n\n---\n"
                "The user has a quick side question. Be concise.\n\n"
                f"Question: {text}"
            )

            options = ClaudeAgentOptions(
                tools=[],          # no tools
                max_turns=1,       # one-shot
                system_prompt=(
                    "You are answering a quick side question about an ongoing "
                    "coding session. Be concise and direct. You have NO tool "
                    "access."
                ),
            )

            response_text = ""
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            response_text += block.text + "\n"

            if response_text.strip():
                self._broadcast_event({
                    "type": "assistant_message",
                    "content": [{"type": "text", "text": response_text.strip()}],
                    "btw": True,
                })

        except Exception as e:
            self._broadcast_event({"type": "btw_end", "error": str(e)})
            return

        self._broadcast_event({"type": "btw_end"})

    def send_input_bytes(self, data: bytes) -> None:
        self.send_input(data.decode("utf-8", errors="replace"))

    def answer_question(self, answer: str) -> None:
        """Provide an answer to a pending question.

        Only the first answer is accepted — late answers from other
        clients are dropped. The question_answered broadcast dismisses
        modals on all clients.
        """
        if not getattr(self, '_question_pending', False):
            return  # Already answered, drop late duplicates
        self._question_pending = False
        if hasattr(self, '_question_answer_queue'):
            self._question_answer_queue.put_nowait(answer)
        # Save to history + broadcast so all clients dismiss their modals.
        # Saving to history lets replay know which questions were answered.
        self._emit_event({
            "type": "question_answered",
            "answer": answer,
        })

    def set_mode(self, mode: str) -> None:
        """Change the agent permission mode at runtime.

        Valid modes: "default", "plan", "acceptEdits", "bypassPermissions".
        """
        import asyncio
        self._current_mode = mode
        self._agent_options["permission_mode"] = mode
        if self._client is not None:
            async def _do_set_mode():
                try:
                    await self._client.set_permission_mode(mode)
                    self._emit_event({
                        "type": "system", "subtype": "debug",
                        "data": {"mode_set": mode, "ok": True},
                    })
                except Exception as e:
                    self._emit_event({
                        "type": "system", "subtype": "debug",
                        "data": {"mode_set": mode, "error": str(e)},
                    })
            try:
                asyncio.ensure_future(_do_set_mode())
            except RuntimeError:
                # No running event loop — try creating a task directly
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(_do_set_mode())
                except Exception:
                    pass
        self._broadcast_settings()

    def set_effort(self, effort: str) -> None:
        """Change the agent effort level at runtime.

        Valid levels: "low", "medium", "high", "max".
        """
        import asyncio
        self._current_effort = effort
        self._agent_options["effort"] = effort  # persist for resume
        if self._client is None:
            self._broadcast_settings()
            return

        async def _do_set_effort():
            try:
                await self._client.set_model(effort=effort)
            except Exception:
                pass

        try:
            asyncio.ensure_future(_do_set_effort())
        except Exception:
            pass
        self._broadcast_settings()

    async def set_model_async(self, model: str) -> None:
        """Change the model at runtime."""
        self._current_model = model
        if self._client is None:
            return
        try:
            await self._client.set_model(model if model != 'default' else None)
        except Exception:
            pass
        self._broadcast_settings()

    async def get_context_usage(self) -> dict | None:
        """Get context window usage breakdown from the SDK."""
        if self._client is not None:
            try:
                return await self._client.get_context_usage()
            except Exception:
                pass
        return None

    async def get_models(self) -> list:
        """Get available models from the SDK."""
        if self._client is not None:
            try:
                info = await self._client.get_server_info()
                if info and 'models' in info:
                    return info['models']
            except Exception:
                pass
        # Fallback — common Claude models
        return [
            {"value": "default", "displayName": "Default (Opus 4.6)", "description": "Most capable, 1M context"},
            {"value": "sonnet", "displayName": "Sonnet 4.6", "description": "Best for everyday tasks"},
            {"value": "haiku", "displayName": "Haiku 4.5", "description": "Fastest for quick answers"},
        ]

    def _broadcast_settings(self) -> None:
        """Broadcast current mode/effort/model to all subscribers."""
        event = {
            "type": "settings",
            "mode": getattr(self, '_current_mode', 'default'),
            "effort": getattr(self, '_current_effort', 'high'),
            "model": getattr(self, '_current_model', 'default'),
        }
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def get_settings(self) -> dict:
        """Return current mode/effort/model for new subscribers."""
        settings: dict = {
            "mode": getattr(self, '_current_mode', 'default'),
            "effort": getattr(self, '_current_effort', 'high'),
            "model": getattr(self, '_current_model', 'default'),
        }
        # If there's a pending question, include it so late-joining clients
        # can show the modal immediately without relying on history replay.
        if getattr(self, '_question_pending', False):
            # Find the last question or plan_review event in history
            for ev in reversed(self._message_history):
                if ev.get("type") == "question":
                    settings["pending_question"] = ev
                    break
                if ev.get("type") == "plan_review":
                    settings["pending_plan_review"] = ev
                    break
        return settings

    def _build_prompt_with_attachments(
        self,
        text: str,
        attachments: list[dict],
    ) -> str:
        """Build a text prompt with attachments saved to temp files.

        Images and binary files are saved to a temp directory so Claude
        can access them via the Read tool. Text files are inlined.
        """
        import base64
        import tempfile
        from pathlib import Path

        parts: list[str] = []
        for att in attachments:
            mime = att.get("type", "application/octet-stream")
            data = att.get("data", "")
            name = att.get("name", "file")
            if mime.startswith("image/"):
                # Save image to temp file so Claude can read it
                try:
                    raw = base64.b64decode(data)
                    tmp_dir = Path(tempfile.gettempdir()) / "be-conductor-uploads"
                    tmp_dir.mkdir(exist_ok=True)
                    tmp_path = tmp_dir / name
                    tmp_path.write_bytes(raw)
                    parts.append(
                        f"I've attached an image. It's saved at: {tmp_path}\n"
                        f"Please use the Read tool to view it."
                    )
                except Exception:
                    parts.append(f"[Attached image: {name} — failed to save]")
            else:
                try:
                    decoded = base64.b64decode(data).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    decoded = "(binary file)"
                parts.append(f"[Attached file: {name}]\n{decoded}")
        if text:
            parts.append(text)
        return "\n\n".join(parts)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def get_message_history(self, offset: int = 0,
                            limit: int | None = None) -> list[dict]:
        """Return structured message history for replay.

        With *offset* and *limit* you can paginate: ``offset`` is the
        start index, ``limit`` the max number of events to return.
        """
        if limit is None:
            return list(self._message_history[offset:])
        return list(self._message_history[offset:offset + limit])

    def get_message_count(self) -> int:
        return len(self._message_history)

    def get_buffer(self) -> bytes:
        return bytes(self._console_buffer)

    def get_buffer_text(self, max_lines: int = 500) -> str:
        raw = self._console_buffer.decode("utf-8", errors="replace")
        clean = _ANSI_RE.sub("", raw)
        lines = clean.splitlines()
        if max_lines and len(lines) > max_lines:
            lines = lines[-max_lines:]
        return "\n".join(lines)

    def get_screen_snapshot(self, clean: bool = False) -> bytes:
        return bytes(self._console_buffer)

    def resize(self, rows: int, cols: int, source: str | None = None,
               client_id: str | None = None) -> None:
        self.rows = rows
        self.cols = cols

    def interrupt(self, timeout: float = 30.0) -> None:
        """Interrupt the current query, or stop the session if graceful-stopping."""
        if self.status == "stopping":
            # Graceful stop: interrupt any running query, then signal the
            # agent loop to exit by changing status and unblocking the queue.
            if self._client:
                asyncio.ensure_future(self._do_interrupt())
            asyncio.ensure_future(self._graceful_shutdown())
            return
        if self._client:
            asyncio.ensure_future(self._do_interrupt())
        # Don't cancel _run_task — the SDK's interrupt will stop the
        # current query and the loop will wait for the next prompt.

    async def _do_interrupt(self) -> None:
        if self._client:
            try:
                await self._client.interrupt()
            except Exception:
                pass

    async def _graceful_shutdown(self) -> None:
        """Signal the agent loop to exit for graceful stop."""
        self._was_graceful = True
        # Unblock _input_queue.get() so the loop can check status and exit
        try:
            self._input_queue.put_nowait({"text": "", "_shutdown": True})
        except Exception:
            pass
        # Give the loop a moment to exit cleanly, then force-cancel.
        # The finally block in _agent_loop handles status and _on_exit.
        await asyncio.sleep(3)
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()

    async def kill(self) -> None:
        self.status = "killed"
        self.exit_code = -9
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
        self._broadcast_close()

    async def cleanup(self) -> None:
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Client tracking (simplified — no resize authority needed)
    # ------------------------------------------------------------------

    def cli_connected(self, client_id: str) -> None:
        self.cli_attach_count += 1
        if client_id:
            self._attached_sources[client_id] = "cli"

    def cli_disconnected(self, client_id: str) -> None:
        self.cli_attach_count = max(0, self.cli_attach_count - 1)
        if client_id:
            self._attached_sources.pop(client_id, None)

    def browser_connected(self, client_id: str, source: str = "browser") -> None:
        if client_id:
            self._attached_sources[client_id] = source

    def browser_disconnected(self, client_id: str) -> None:
        if client_id:
            self._attached_sources.pop(client_id, None)

    @property
    def attached_clients(self) -> list[dict]:
        return [
            {"client_id": cid, "source": src}
            for cid, src in self._attached_sources.items()
        ]

    @property
    def live_cwd(self) -> str | None:
        return self.cwd

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "session_type": self.session_type,
            "name": self.name,
            "command": self.command,
            "status": self.status,
            "pid": self.pid,
            "start_time": self.start_time,
            "created_at": self.created_at,
            "exit_code": self.exit_code,
            "cwd": self.live_cwd,
            "rows": self.rows,
            "cols": self.cols,
            "resize_source": self.resize_source,
            "resize_owner": self.resize_owner_id or self.browser_resize_owner_id,
            "cli_attach_count": self.cli_attach_count,
            "attached_clients": self.attached_clients,
            "message_count": len(self._message_history),
        }
        if self.resume_id:
            d["resume_id"] = self.resume_id
        if self.worktree:
            d["worktree"] = self.worktree
        return d
