# be-conductor — Local orchestration for terminal sessions.
#
# Copyright (c) 2026 Max Rheiner / Somniacs AG
#
# Licensed under the MIT License. You may obtain a copy
# of the license at:
#
#     https://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.

"""Headless task runs — an agent command run to completion for its answer.

A headless run is an ordinary PTY session (so it shows up in the dashboard,
can be watched, and can be answered if it stalls) whose command line is
built from the command's ``headless`` block and whose output is parsed for
the final result, cost and token usage.
"""

import asyncio
import json
import logging
import os
import shlex
import time
from datetime import datetime, timezone

from be_conductor.sessions.session import Session, _ANSI_RE
from be_conductor.utils import config as cfg

logger = logging.getLogger(__name__)

TASKS_DIR = cfg.CONDUCTOR_DIR / "tasks"

# Built-in headless blocks, used when a command says `headless: true` or has
# no block of its own.  Keyed by the command's executable.
#
#   format   json  — one JSON document (last parsable line wins)
#            jsonl — an event stream: result = last match, cost/tokens summed
#            text  — no structure: result = the ANSI-stripped output
#   result_match restricts which events may carry the result.
#   error_match marks an event as a failure even when the CLI exits 0
#   (OpenCode does); error_message_json_path is its human-readable text.
HEADLESS_PRESETS: dict[str, dict] = {
    "claude": {
        "args": ["-p", "{prompt}", "--output-format", "json"],
        "format": "json",
        "result_json_path": "result",
        "cost_json_path": "total_cost_usd",
        "tokens_json_path": "usage",
        "error_json_path": "is_error",
        "session_json_path": "session_id",
    },
    "codex": {
        "args": ["exec", "{prompt}", "--json", "--skip-git-repo-check"],
        "format": "jsonl",
        "result_json_path": "item.text",
        "result_match": {"item.type": "agent_message"},
        "tokens_json_path": "usage",
        "error_match": {"type": "turn.failed"},
        "error_message_json_path": "error.message",
    },
    "opencode": {
        "args": ["run", "{prompt}", "--model", "{model}", "--format", "json"],
        "format": "jsonl",
        "result_json_path": "part.text",
        "result_match": {"type": "text"},
        "cost_json_path": "part.cost",
        "tokens_json_path": "part.tokens",
        "error_match": {"type": "error"},
        "error_message_json_path": "error.data.message",
    },
}

# The trivial prompt `profile check` runs.
CHECK_PROMPT = "Reply with the single word OK and nothing else."

_MAX_TEXT_RESULT = 64_000


class HeadlessError(ValueError):
    """The command cannot be run headless."""


# ── Command lookup / argv ─────────────────────────────────────────────────

def _base_exe(command: str) -> str:
    try:
        return os.path.basename(shlex.split(command)[0])
    except (ValueError, IndexError):
        return ""


def find_command(ref: str, profile: str | None = None) -> dict | None:
    """Resolve *ref* (a label, or a command string) to an allowed_commands entry.

    Labels win.  For a command string, an entry bound to *profile* is
    preferred over an unbound one.
    """
    entries = [e for e in cfg.ALLOWED_COMMANDS if isinstance(e, dict) and e.get("command")]
    for e in entries:
        if e.get("label") == ref:
            return e
    matches = [e for e in entries if e["command"] == ref]
    if not matches:
        matches = [e for e in entries if _base_exe(e["command"]) == _base_exe(ref) and
                   len(shlex.split(ref)) == 1]
    if profile:
        for e in matches:
            if e.get("profile") == profile:
                return e
    for e in matches:
        if not e.get("profile"):
            return e
    return matches[0] if matches else None


def headless_block(entry: dict) -> dict | None:
    """The effective headless block for a command entry (preset-merged)."""
    block = entry.get("headless")
    preset = HEADLESS_PRESETS.get(_base_exe(entry["command"]))
    if isinstance(block, dict):
        if "args" not in block:
            if not preset:
                return None
            return {**preset, **block}
        out = dict(block)
        if "format" not in out:
            out["format"] = "json" if out.get("result_json_path") else "text"
        return out
    # `headless: true`, or nothing — fall back to the preset if there is one.
    return dict(preset) if preset else None


def is_exposed_headless(entry: dict) -> bool:
    """True when the entry opted into headless use (an explicit block/flag)."""
    return bool(entry.get("headless")) and headless_block(entry) is not None


def build_argv(entry: dict, prompt: str, model: str | None = None) -> list[str]:
    block = headless_block(entry)
    if not block:
        raise HeadlessError(
            f"command '{entry.get('label') or entry['command']}' has no headless block")
    argv = shlex.split(entry["command"])
    values = {"prompt": prompt, "model": model or ""}
    args = list(block["args"])
    i = 0
    while i < len(args):
        arg = str(args[i])
        # An optional placeholder with no value drops itself and its flag:
        # ["--model", "{model}"] vanishes when the profile names no model.
        if arg == "{model}" and not values["model"]:
            if argv and i > 0 and str(args[i - 1]).startswith("-"):
                argv.pop()
            i += 1
            continue
        for key, val in values.items():
            arg = arg.replace("{" + key + "}", val)
        argv.append(arg)
        i += 1
    return argv


# ── Output collector ──────────────────────────────────────────────────────

def _dig(obj, path: str):
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def _flatten_numbers(obj, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}{k}"
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[key] = v
            elif isinstance(v, dict):
                out.update(_flatten_numbers(v, key + "."))
    return out


class HeadlessCollector:
    """Incrementally parses a headless run's output.

    Works line by line as bytes arrive, so a long event stream is never
    lost to the session's rolling output buffer.
    """

    def __init__(self, block: dict):
        self.block = block
        self.format = block.get("format", "text")
        self.result: str | None = None
        self.cost_usd: float | None = None
        self.tokens: dict[str, float] = {}
        self.is_error = False
        self.error_message: str | None = None
        self.agent_session_id: str | None = None
        self._partial = b""
        self._text = bytearray()

    def feed(self, data: bytes):
        if self.format == "text":
            self._text.extend(data)
            if len(self._text) > _MAX_TEXT_RESULT * 4:
                del self._text[:-_MAX_TEXT_RESULT * 2]
            return
        buf = self._partial + data
        *lines, self._partial = buf.split(b"\n")
        for line in lines:
            self._line(line)

    def finish(self):
        if self.format == "text":
            text = _ANSI_RE.sub("", self._text.decode("utf-8", errors="replace"))
            text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
            self.result = text[-_MAX_TEXT_RESULT:] or None
            return
        if self._partial:
            self._line(self._partial)
            self._partial = b""

    def _line(self, raw: bytes):
        text = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).strip()
        start = text.find("{")
        if start < 0:
            return
        try:
            obj = json.loads(text[start:])
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        b = self.block
        summed = self.format == "jsonl"

        match = b.get("result_match") or {}
        if all(_dig(obj, k) == v for k, v in match.items()):
            value = _dig(obj, b["result_json_path"]) if b.get("result_json_path") else None
            if isinstance(value, str) and value.strip():
                self.result = value

        if b.get("cost_json_path"):
            cost = _dig(obj, b["cost_json_path"])
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                self.cost_usd = (self.cost_usd or 0.0) + cost if summed else float(cost)

        if b.get("tokens_json_path"):
            tok = _flatten_numbers(_dig(obj, b["tokens_json_path"]))
            if tok:
                if summed:
                    for k, v in tok.items():
                        self.tokens[k] = self.tokens.get(k, 0) + v
                else:
                    self.tokens = tok

        if b.get("error_json_path") and _dig(obj, b["error_json_path"]):
            self.is_error = True
        err_match = b.get("error_match")
        if err_match and all(_dig(obj, k) == v for k, v in err_match.items()):
            self.is_error = True
            if b.get("error_message_json_path"):
                text = _dig(obj, b["error_message_json_path"])
                if isinstance(text, str) and text.strip():
                    self.error_message = text.strip()
        if b.get("session_json_path"):
            sid = _dig(obj, b["session_json_path"])
            if isinstance(sid, str):
                self.agent_session_id = sid


# ── Session ───────────────────────────────────────────────────────────────

class HeadlessSession(Session):
    """A PTY session that runs one prompt to completion."""

    def __init__(self, *args, prompt: str, block: dict, label: str | None = None,
                 timeout_seconds: float | None = None,
                 max_cost_usd: float | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.headless = True
        self.prompt = prompt
        self.label = label
        self.timeout_seconds = timeout_seconds
        self.max_cost_usd = max_cost_usd
        self.task_status = "running"
        self.fail_reason: str | None = None
        self.result: str | None = None
        self.cost_usd: float | None = None
        self.tokens: dict = {}
        self.finished_at: str | None = None
        self._collector = HeadlessCollector(block)
        self._timeout_task: asyncio.Task | None = None
        self._done = asyncio.Event()
        # A finished task can be continued interactively when the agent
        # reported its own session id (Claude Code does).
        self._can_continue = bool(block.get("session_json_path") and self.resume_flag)

    async def start(self, rows: int = 24, cols: int = 80):
        # Wide terminal: nothing here wraps, but some CLIs size their own
        # output to the reported width.
        await super().start(rows=rows, cols=max(cols, 200))
        if self.timeout_seconds:
            self._timeout_task = asyncio.create_task(self._enforce_timeout())

    def _append_buffer(self, data: bytes):
        super()._append_buffer(data)
        if self.task_status == "needs_input":
            self.task_status = "running"   # it moved on — someone answered
        self._collector.feed(data)
        if (self.max_cost_usd and self._collector.cost_usd is not None
                and self._collector.cost_usd > self.max_cost_usd
                and not self.fail_reason):
            self.cost_usd = self._collector.cost_usd
            self._abort("cost_cap")

    def on_needs_input(self, _reason: str = ""):
        """Notifier callback — the run is waiting on a prompt."""
        if self.task_status == "running":
            self.task_status = "needs_input"

    def _extract_resume_id(self):
        # Only the agent-reported session id makes a finished task resumable;
        # the inherited buffer scan would match stray text in the answer.
        self._collector.finish()
        if self._can_continue and self._collector.agent_session_id:
            self.resume_id = self._collector.agent_session_id

    def _abort(self, reason: str):
        self.fail_reason = reason
        self.pty.kill()
        task = asyncio.ensure_future(self._kill_hard_later())
        task.add_done_callback(self._log_task_exception)

    async def _kill_hard_later(self, grace: float = 5.0):
        await asyncio.sleep(grace)
        # Only while the leader is still alive: once it has been reaped its
        # pid (= the group id) is free for reuse, and SIGKILL must never
        # land on a stranger.
        if self.pty.poll() is None:
            self.pty.kill_hard()

    async def kill(self):
        await super().kill()
        # A hard stop cancels the monitor before it can finalize.
        if not self.fail_reason:
            self.fail_reason = "stopped"
        self._finalize()

    async def _enforce_timeout(self):
        try:
            await asyncio.sleep(self.timeout_seconds)
        except asyncio.CancelledError:
            return
        if self.pty.poll() is None and not self.fail_reason:
            logger.info("Headless run '%s' timed out after %ss", self.name, self.timeout_seconds)
            self._abort("timeout")

    def _finalize(self):
        if self._done.is_set():
            return
        if self._timeout_task:
            self._timeout_task.cancel()
        c = self._collector
        c.finish()
        self.result = c.result
        self.cost_usd = c.cost_usd if c.cost_usd is not None else self.cost_usd
        self.tokens = c.tokens
        self.finished_at = datetime.now(timezone.utc).isoformat()

        if self.fail_reason == "timeout":
            self.task_status = "timeout"
        elif self.fail_reason:
            self.task_status = "failed"
        elif self.exit_code not in (0, None) or c.is_error:
            self.task_status = "failed"
            self.fail_reason = "agent_error" if c.is_error else f"exit_code_{self.exit_code}"
        elif self.max_cost_usd and (self.cost_usd or 0) > self.max_cost_usd:
            self.task_status = "failed"
            self.fail_reason = "cost_cap"
        else:
            self.task_status = "done"

        # A structured run that never produced its result event still owes
        # the caller an explanation: the agent's own error text if it gave
        # one, else whatever it printed.
        if self.result is None:
            self.result = (c.error_message
                           or self.get_buffer_text(max_lines=60).strip() or None)
        elif c.is_error and c.error_message and c.error_message not in self.result:
            self.result = f"{self.result}\n\n[agent error] {c.error_message}"

        save_task_record(self.task_record())
        if self.profile:
            from be_conductor.profiles import ledger
            ledger.record(self.profile, self.id, self.cost_usd, self.tokens,
                          status=self.task_status, duration_s=self.duration_s)
        self._done.set()

    async def wait(self, timeout: float | None = None) -> bool:
        """Block until the run has finished. False if *timeout* hit first."""
        try:
            await asyncio.wait_for(self._done.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def duration_s(self) -> float | None:
        if not self.start_time:
            return None
        if self.finished_at:
            end = datetime.fromisoformat(self.finished_at).timestamp()
        else:
            end = time.time()
        return max(0.0, end - self.start_time)

    def task_record(self) -> dict:
        """The full task view, including the result text."""
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "profile": self.profile,
            "cwd": self.cwd,
            "prompt": self.prompt,
            "task_status": self.task_status,
            "fail_reason": self.fail_reason,
            "result": self.result,
            "cost_usd": self.cost_usd,
            "tokens": self.tokens or None,
            "exit_code": self.exit_code,
            "started_at": self.created_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 1) if self.duration_s is not None else None,
            "worktree": self.worktree,
        }

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update({
            "headless": True,
            "label": self.label,
            "prompt": self.prompt[:500],
            "task_status": self.task_status,
            "cost_usd": self.cost_usd,
            "started_at": self.created_at,
            "finished_at": self.finished_at,
        })
        if self.fail_reason:
            d["fail_reason"] = self.fail_reason
        if self.result:
            d["result_preview"] = self.result[:300]
        return d


# ── Finished-task records ─────────────────────────────────────────────────

def _task_path(session_id: str):
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in session_id)
    return TASKS_DIR / f"{safe}.json"


def save_task_record(record: dict):
    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        _task_path(record["id"]).write_text(json.dumps(record, indent=2))
    except OSError as e:
        logger.warning("Could not save task record %s: %s", record.get("id"), e)


def load_task_record(session_id: str) -> dict | None:
    try:
        return json.loads(_task_path(session_id).read_text())
    except (OSError, ValueError):
        return None


def delete_task_record(session_id: str):
    _task_path(session_id).unlink(missing_ok=True)
