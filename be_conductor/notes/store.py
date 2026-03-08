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

"""SQLite-backed notes storage at ~/.be-conductor/notes.db."""

import os
import sqlite3
from datetime import datetime, timezone

from be_conductor.utils.config import NOTES_DB

_initialized = False


def _get_conn() -> sqlite3.Connection:
    """Open a connection to the notes database, creating the schema if needed."""
    global _initialized
    NOTES_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(NOTES_DB))
    conn.row_factory = sqlite3.Row
    if not _initialized:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id         TEXT PRIMARY KEY,
                content    TEXT NOT NULL,
                scope      TEXT NOT NULL DEFAULT 'global',
                project_id TEXT,
                session_id TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notes_scope ON notes(scope);
            CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id);
            CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(project_id);
        """)
        _initialized = True
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def list_notes(
    scope: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    q: str | None = None,
) -> list[dict]:
    conn = _get_conn()
    clauses, params = [], []
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if q:
        clauses.append("content LIKE ?")
        params.append(f"%{q}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM notes{where} ORDER BY sort_order ASC, created_at DESC",
        params,
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def create_note(
    content: str,
    scope: str = "global",
    project_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    note_id = os.urandom(4).hex()
    now = _now()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO notes (id, content, scope, project_id, session_id, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
        (note_id, content, scope, project_id, session_id, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_note(note_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def update_note(note_id: str, **fields) -> dict | None:
    allowed = {"content", "scope", "project_id", "session_id"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_note(note_id)
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [note_id]
    conn = _get_conn()
    conn.execute(f"UPDATE notes SET {set_clause} WHERE id = ?", params)
    conn.commit()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def delete_note(note_id: str) -> bool:
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def delete_by_session(session_id: str) -> int:
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM notes WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
    return cursor.rowcount


def delete_by_project(project_id: str) -> int:
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM notes WHERE project_id = ?", (project_id,))
    conn.commit()
    conn.close()
    return cursor.rowcount


def delete_all() -> int:
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM notes")
    conn.commit()
    conn.close()
    return cursor.rowcount


def reorder(note_ids: list[str]) -> None:
    conn = _get_conn()
    for i, nid in enumerate(note_ids):
        conn.execute("UPDATE notes SET sort_order = ? WHERE id = ?", (i, nid))
    conn.commit()
    conn.close()


def cleanup_orphaned(valid_session_ids: set[str]) -> int:
    """Delete session-scoped notes whose session_id is not in *valid_session_ids*."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, session_id FROM notes WHERE scope = 'session' AND session_id IS NOT NULL"
    ).fetchall()
    orphaned = [r["id"] for r in rows if r["session_id"] not in valid_session_ids]
    if not orphaned:
        conn.close()
        return 0
    placeholders = ",".join("?" * len(orphaned))
    cursor = conn.execute(f"DELETE FROM notes WHERE id IN ({placeholders})", orphaned)
    conn.commit()
    conn.close()
    return cursor.rowcount
