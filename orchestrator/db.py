"""SQLite persistence for sessions and messages."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,      -- the Claude Code session UUID
    project         TEXT NOT NULL,
    project_name    TEXT NOT NULL,
    cwd             TEXT NOT NULL,
    profile         TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'queued',
    claude_started  INTEGER NOT NULL DEFAULT 0,
    branch          TEXT,
    model           TEXT,                  -- per-session override of the profile
    effort          TEXT,                  -- low | medium | high | xhigh | max
    permission_mode TEXT,                  -- manual | acceptEdits | plan | auto | ...
    turns           INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,   -- user | assistant | thinking | tool | tool_result | system | result | error
    text        TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
"""


def init(path: Path) -> None:
    global _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(SCHEMA)
        # Columns added after the first release: existing databases predate them.
        have = {r["name"] for r in _conn.execute("PRAGMA table_info(sessions)")}
        for col in ("model", "effort", "permission_mode"):
            if col not in have:
                _conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
        _conn.commit()
        # Any session left mid-flight by a restart is not actually running.
        _conn.execute(
            "UPDATE sessions SET status='interrupted', updated_at=? "
            "WHERE status IN ('running','queued')",
            (time.time(),),
        )
        _conn.commit()


def _c() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db.init() has not been called")
    return _conn


def create_session(
    sid: str, project: str, project_name: str, cwd: str, profile: str, title: str,
    branch: str | None, model: str | None = None, effort: str | None = None,
    permission_mode: str | None = None,
) -> None:
    now = time.time()
    with _lock:
        _c().execute(
            "INSERT INTO sessions (id, project, project_name, cwd, profile, title, status, branch,"
            " model, effort, permission_mode, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?,?)",
            (sid, project, project_name, cwd, profile, title, branch, model, effort,
             permission_mode, now, now),
        )
        _c().commit()


def update_session(sid: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _c().execute(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))
        _c().commit()


def rename_session(sid: str, title: str) -> None:
    """Set the title without touching updated_at.

    update_session() bumps updated_at, which orders the conversation list --
    renaming a conversation should not jump it to the top as if it had just
    been used.
    """
    with _lock:
        _c().execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))
        _c().commit()


def bump_session(sid: str, cost_delta: float, turns_delta: int) -> None:
    with _lock:
        _c().execute(
            "UPDATE sessions SET cost_usd = cost_usd + ?, turns = turns + ?, updated_at = ?"
            " WHERE id=?",
            (cost_delta, turns_delta, time.time(), sid),
        )
        _c().commit()


def get_session(sid: str) -> dict | None:
    with _lock:
        row = _c().execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return dict(row) if row else None


def list_sessions(limit: int = 50) -> list[dict]:
    with _lock:
        rows = _c().execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_session(sid: str) -> None:
    with _lock:
        _c().execute("DELETE FROM messages WHERE session_id=?", (sid,))
        _c().execute("DELETE FROM sessions WHERE id=?", (sid,))
        _c().commit()


def add_message(sid: str, role: str, text: str = "", meta: dict | None = None) -> int:
    now = time.time()
    with _lock:
        cur = _c().execute(
            "INSERT INTO messages (session_id, role, text, meta, created_at) VALUES (?,?,?,?,?)",
            (sid, role, text, json.dumps(meta or {}), now),
        )
        _c().execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sid))
        _c().commit()
        return int(cur.lastrowid)


def get_messages(sid: str, after: int = 0, limit: int = 500) -> list[dict]:
    with _lock:
        rows = _c().execute(
            "SELECT * FROM messages WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
            (sid, after, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d["meta"])
        except (TypeError, ValueError):
            d["meta"] = {}
        out.append(d)
    return out


def last_user_text(sid: str) -> str:
    with _lock:
        row = _c().execute(
            "SELECT text FROM messages WHERE session_id=? AND role='user' ORDER BY seq LIMIT 1",
            (sid,),
        ).fetchone()
    return row["text"] if row else ""
