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

"""Export notes to Markdown."""

from datetime import datetime, timezone

from be_conductor.notes import store


def export_markdown(
    scope: str | None = None,
    project_id: str | None = None,
) -> str:
    """Export notes as a Markdown document, grouped by scope."""
    notes = store.list_notes(scope=scope, project_id=project_id)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Notes\n", f"Exported from be-conductor on {now}\n"]

    groups: dict[str, list[dict]] = {}
    for n in notes:
        key = n["scope"]
        if key == "session" and n.get("session_id"):
            key = f"session:{n['session_id']}"
        elif key == "project" and n.get("project_id"):
            key = f"project:{n['project_id']}"
        groups.setdefault(key, []).append(n)

    for key in ("global",):
        if key in groups:
            lines.append("\n## Global\n")
            for n in groups.pop(key):
                ts = _fmt_ts(n["created_at"])
                lines.append(f"- {n['content']} ({ts})")

    for key in sorted(k for k in groups if k.startswith("project:")):
        pid = key.split(":", 1)[1]
        lines.append(f"\n## Project: {pid}\n")
        for n in groups.pop(key):
            ts = _fmt_ts(n["created_at"])
            lines.append(f"- {n['content']} ({ts})")

    for key in sorted(k for k in list(groups)):
        sid = key.split(":", 1)[1] if ":" in key else key
        lines.append(f"\n## Session: {sid}\n")
        for n in groups.pop(key):
            ts = _fmt_ts(n["created_at"])
            lines.append(f"- {n['content']} ({ts})")

    return "\n".join(lines) + "\n"


def _fmt_ts(iso: str) -> str:
    """Format an ISO timestamp as a short date-time string."""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso
