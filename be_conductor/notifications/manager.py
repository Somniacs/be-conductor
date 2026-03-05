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

"""Notification manager — dispatches notification events to registered handlers."""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable

import pyte

from be_conductor.utils.config import CONDUCTOR_DIR

log = logging.getLogger("uvicorn.error")

# File for persisting per-device notification settings (webhook URLs etc.)
_NOTIFICATIONS_FILE = CONDUCTOR_DIR / "notifications.json"

# ── Default notification patterns ─────────────────────────────────────────
# These match common prompts across AI agents.  Per-agent overrides can be
# configured via ``notification_patterns`` in the command config.

_DEFAULT_PATTERNS = [
    re.compile(r"\(y\)es.*\(n\)o", re.IGNORECASE),
    re.compile(r"\[Y/n\]", re.IGNORECASE),
    re.compile(r"\[y/N\]", re.IGNORECASE),
    re.compile(r"\(y/n\)", re.IGNORECASE),
    re.compile(r"(?:allow|deny|approve|reject).*\?", re.IGNORECASE),
    re.compile(r"(?:do you want|would you like|shall I|should I|proceed|continue|confirm).*\?", re.IGNORECASE),
    # Claude Code selection menus — match the hint lines at the bottom
    re.compile(r"Enter to select.*↑/↓ to navigate"),
    re.compile(r"Esc to cancel.*Tab to amend"),
]

# Minimum seconds between notifications for the same session+reason.
_COOLDOWN_SECONDS = 60

# Seconds of silence after output before scanning for patterns.
_SILENCE_SECONDS = 5

# Minimum output bytes before we consider scanning (avoids keepalive noise).
_MIN_OUTPUT_BYTES = 100


class NotificationEvent:
    """A single notification event from a session."""

    __slots__ = ("session_id", "session_name", "reason", "snippet", "timestamp")

    def __init__(self, session_id: str, session_name: str, reason: str,
                 snippet: str, timestamp: float | None = None):
        self.session_id = session_id
        self.session_name = session_name
        self.reason = reason
        self.snippet = snippet
        self.timestamp = timestamp or time.time()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "session_name": self.session_name,
            "reason": self.reason,
            "snippet": self.snippet,
            "timestamp": self.timestamp,
        }


class NotificationManager:
    """Central dispatcher for notification events.

    Receives events from sessions and fans out to registered handlers
    (WebSocket broadcast, webhooks, native OS, etc.).
    """

    def __init__(self):
        self._handlers: list[Callable] = []
        self._device_settings: dict[str, dict] = {}
        self._webhook_settings: dict = {}  # Global webhook config
        self._load_settings()

    def register_handler(self, handler: Callable):
        """Register an async handler that receives NotificationEvent objects."""
        self._handlers.append(handler)

    async def notify(self, event: NotificationEvent):
        """Dispatch a notification event to all registered handlers."""
        for handler in self._handlers:
            try:
                await handler(event)
            except Exception as e:
                log.warning("Notification handler error: %s", e)

    # ── Per-device settings ───────────────────────────────────────────────

    def get_device_settings(self, device_id: str) -> dict:
        return self._device_settings.get(device_id, {})

    def set_device_settings(self, device_id: str, settings: dict):
        self._device_settings[device_id] = settings
        self._save_settings()

    def get_all_device_settings(self) -> dict:
        return dict(self._device_settings)

    # ── Global webhook settings ────────────────────────────────────────────

    def get_webhook_settings(self) -> dict:
        return dict(self._webhook_settings)

    def set_webhook_settings(self, settings: dict):
        self._webhook_settings = settings
        self._save_settings()

    def _load_settings(self):
        if _NOTIFICATIONS_FILE.exists():
            try:
                data = json.loads(_NOTIFICATIONS_FILE.read_text())
                self._device_settings = data.get("devices", {})
                self._webhook_settings = data.get("webhook", {})
            except Exception:
                pass

    def _save_settings(self):
        CONDUCTOR_DIR.mkdir(parents=True, exist_ok=True)
        _NOTIFICATIONS_FILE.write_text(json.dumps(
            {"devices": self._device_settings, "webhook": self._webhook_settings},
            indent=2,
        ))


class SessionNotifier:
    """Tracks output activity for a single session and fires notifications.

    Uses a pyte virtual terminal to maintain a clean screen representation,
    exactly like what xterm.js renders in the browser.  Pattern matching
    runs against this clean text instead of raw escape-laden bytes.
    """

    def __init__(self, session_id: str, session_name: str,
                 manager: NotificationManager,
                 patterns: list[re.Pattern] | None = None,
                 ansi_re: re.Pattern | None = None):
        self.session_id = session_id
        self.session_name = session_name
        self._manager = manager
        self._patterns = patterns or list(_DEFAULT_PATTERNS)

        # Virtual terminal — gives us clean screen text
        self._screen = pyte.Screen(200, 50)
        self._stream = pyte.Stream(self._screen)

        # Activity tracking
        self._output_bytes = 0
        self._last_output_time = 0.0
        self._silence_handle: asyncio.TimerHandle | None = None
        self._cooldowns: dict[str, float] = {}  # reason → last notify time
        self._loop: asyncio.AbstractEventLoop | None = None

    def on_output(self, data: bytes, buffer: bytearray):
        """Called when new output arrives from the session."""
        self._output_bytes += len(data)
        self._last_output_time = time.time()

        # Feed data to the virtual terminal
        try:
            self._stream.feed(data.decode("utf-8", errors="replace"))
        except Exception:
            pass

        # Cancel any pending silence check and schedule a new one.
        if self._silence_handle is not None:
            self._silence_handle.cancel()

        if self._loop is None:
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                return

        self._silence_handle = self._loop.call_later(
            _SILENCE_SECONDS,
            lambda: asyncio.ensure_future(self._check_patterns()),
        )

    # Characters that indicate TUI chrome / box drawing — not real text.
    _CHROME_CHARS = set("─│┌┐└┘├┤┬┴┼━┃╋═║╔╗╚╝╠╣╦╩╬▀▄█▌▐░▒▓")

    def _get_screen_text(self) -> list[str]:
        """Get non-empty lines from the virtual terminal screen.

        Filters out lines that are purely TUI chrome (box-drawing chars,
        borders) which are not meaningful for pattern matching.
        """
        result = []
        for line in self._screen.display:
            stripped = line.rstrip()
            if not stripped.strip():
                continue
            # Skip lines that are purely box-drawing / UI borders
            text_chars = [c for c in stripped.strip() if c not in self._CHROME_CHARS]
            if not text_chars:
                continue
            result.append(stripped)
        return result

    async def _check_patterns(self):
        """Scan the bottom of the virtual screen for notification patterns.

        Checks the last 5 non-empty lines — AI agent prompts (especially
        Claude Code's selection menus) can span several lines:
        question line + option lines + hint line.
        """
        if self._output_bytes < _MIN_OUTPUT_BYTES:
            return

        try:
            lines = self._get_screen_text()
            # Bottom 5 lines — covers question + selection options + hints.
            bottom = lines[-5:] if len(lines) >= 5 else lines
            recent = "\n".join(bottom)

            log.info("Notification scan [%s]: bottom=%r", self.session_id, recent[:300])

            reason, matched_line = self._match_patterns(bottom)
            if not reason:
                return

            # Cooldown check — keyed on reason + snippet so a *new* prompt
            # with the same reason category still fires immediately.
            cooldown_key = reason + ":" + matched_line.strip()[:80]
            now = time.time()
            last = self._cooldowns.get(cooldown_key, 0)
            if now - last < _COOLDOWN_SECONDS:
                return
            self._cooldowns[cooldown_key] = now

            # Reset output counter so the same output doesn't re-trigger
            self._output_bytes = 0

            # Use the matched line as snippet (not the last screen line)
            snippet = matched_line.strip()[:120]
            event = NotificationEvent(
                session_id=self.session_id,
                session_name=self.session_name,
                reason=reason,
                snippet=snippet,
            )
            log.info("Notification fired [%s]: reason=%s snippet=%s",
                     self.session_id, reason, snippet[:80])
            await self._manager.notify(event)
        except Exception as e:
            log.warning("Notification pattern check failed: %s", e)

    # Lines that are UI chrome / hints — not useful as notification snippets.
    _HINT_RE = re.compile(
        r"(Enter to select|Esc to cancel|↑/↓ to navigate|Tab to amend|ctrl\+e to explain)",
        re.IGNORECASE,
    )

    def _match_patterns(self, lines: list[str]) -> tuple[str | None, str]:
        """Match lines against notification patterns.

        Returns (reason, snippet) or (None, "") if no match.
        The snippet is the most informative nearby line (skipping UI hints).
        """
        for i, line in enumerate(lines):
            for pattern in self._patterns:
                if pattern.search(line):
                    src = pattern.pattern.lower()
                    if "y" in src and "n" in src:
                        reason = "Needs confirmation"
                    elif "enter to select" in src or "esc to cancel" in src:
                        reason = "Needs confirmation"
                    elif "allow" in src or "deny" in src or "approve" in src:
                        reason = "Asking for permission"
                    elif "?" in src:
                        reason = "Asking a question"
                    else:
                        reason = "Needs attention"

                    # Pick the best snippet: walk upward from the matched
                    # line to find something more descriptive than a UI hint.
                    snippet = line
                    for j in range(i, -1, -1):
                        candidate = lines[j].strip()
                        if candidate and not self._HINT_RE.search(candidate):
                            snippet = candidate
                            break

                    return reason, snippet
        return None, ""

    def cancel(self):
        """Cancel any pending silence check."""
        if self._silence_handle is not None:
            self._silence_handle.cancel()
