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

"""Webhook notification sender — Telegram, Discord, Slack, and generic JSON."""

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

# Timeout for webhook HTTP requests.
_TIMEOUT = 10.0


def _detect_platform(url: str) -> str:
    """Detect webhook platform from URL."""
    host = urlparse(url).hostname or ""
    if "telegram.org" in host:
        return "telegram"
    if "discord.com" in host or "discordapp.com" in host:
        return "discord"
    if "slack.com" in host:
        return "slack"
    return "generic"


def _format_telegram(session_name: str, reason: str, snippet: str,
                     url: str, chat_id: str | None = None,
                     dashboard_url: str = "") -> tuple[str, dict]:
    """Format a Telegram bot API message.

    URL format: https://api.telegram.org/bot<TOKEN>/sendMessage
    If chat_id is provided separately, it's used; otherwise it must be
    embedded in the stored settings.
    """
    text = f"🔔 *{session_name}*: {reason}"
    if snippet:
        text += f"\n`{snippet}`"
    if dashboard_url:
        text += f"\n[Open session]({dashboard_url})"

    payload = {"text": text, "parse_mode": "Markdown"}
    if chat_id:
        payload["chat_id"] = chat_id
    return url, payload


def _format_discord(session_name: str, reason: str, snippet: str,
                    url: str, dashboard_url: str = "") -> tuple[str, dict]:
    """Format a Discord webhook message."""
    content = f"🔔 **{session_name}**: {reason}"
    if snippet:
        content += f"\n```{snippet}```"
    if dashboard_url:
        content += f"\n[Open session]({dashboard_url})"
    return url, {"content": content}


def _format_slack(session_name: str, reason: str, snippet: str,
                  url: str, dashboard_url: str = "") -> tuple[str, dict]:
    """Format a Slack incoming webhook message."""
    text = f"🔔 *{session_name}*: {reason}"
    if snippet:
        text += f"\n```{snippet}```"
    if dashboard_url:
        text += f"\n<{dashboard_url}|Open session>"
    return url, {"text": text}


def _format_generic(session_name: str, reason: str, snippet: str,
                    url: str, dashboard_url: str = "") -> tuple[str, dict]:
    """Format a generic JSON POST."""
    payload: dict[str, str] = {
        "session": session_name,
        "reason": reason,
        "snippet": snippet,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }
    if dashboard_url:
        payload["dashboard_url"] = dashboard_url
    return url, payload


async def send_webhook(url: str, session_name: str, reason: str,
                       snippet: str = "", chat_id: str | None = None,
                       dashboard_url: str = "") -> bool:
    """Send a notification to a webhook URL.

    Auto-detects the platform (Telegram, Discord, Slack) from the URL
    and formats the message accordingly.

    Returns True on success, False on failure.
    """
    if not url:
        return False

    platform = _detect_platform(url)

    if platform == "telegram":
        post_url, payload = _format_telegram(session_name, reason, snippet, url, chat_id, dashboard_url)
    elif platform == "discord":
        post_url, payload = _format_discord(session_name, reason, snippet, url, dashboard_url)
    elif platform == "slack":
        post_url, payload = _format_slack(session_name, reason, snippet, url, dashboard_url)
    else:
        post_url, payload = _format_generic(session_name, reason, snippet, url, dashboard_url)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(post_url, json=payload)
            if resp.status_code < 300:
                log.info("Webhook sent to %s for session '%s': %s", platform, session_name, reason)
                return True
            else:
                log.warning("Webhook %s returned %d: %s", platform, resp.status_code, resp.text[:200])
                return False
    except Exception as e:
        log.warning("Webhook send failed (%s): %s", platform, e)
        return False


async def test_webhook(url: str, chat_id: str | None = None) -> tuple[bool, str]:
    """Send a test message to verify webhook configuration.

    Returns (success, message).
    """
    ok = await send_webhook(
        url=url,
        session_name="Test",
        reason="This is a test notification from Be-Conductor",
        snippet="If you see this, webhook notifications are working.",
        chat_id=chat_id,
    )
    if ok:
        return True, "Test notification sent successfully"
    return False, "Failed to send test notification — check the URL"
