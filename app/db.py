"""SQLite persistence layer (stdlib only)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Optional

DB_PATH = os.environ.get("AGENDA_DB", os.path.join(os.path.dirname(__file__), "..", "data", "agenda.db"))

_lock = threading.RLock()


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                planned_minutes INTEGER NOT NULL CHECK (planned_minutes > 0),
                minimum_minutes INTEGER NOT NULL CHECK (minimum_minutes >= 0),
                priority INTEGER NOT NULL DEFAULT 3,
                incompressible INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                actual_end TEXT,
                postponed INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS breaks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT '休息',
                fixed_start TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0)
            );

            CREATE TABLE IF NOT EXISTS undo_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # migrate databases created before the postponement feature
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        if "postponed" not in cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN postponed INTEGER NOT NULL DEFAULT 0")


# ---------------------------------------------------------------------------
# meetings
# ---------------------------------------------------------------------------

def create_meeting(title: str, start_time: str, end_time: str) -> dict:
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO meetings (title, start_time, end_time, created_at) VALUES (?, ?, ?, ?)",
            (title, start_time, end_time, datetime.now().isoformat(timespec="minutes")),
        )
        row = conn.execute("SELECT * FROM meetings WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def get_meeting(meeting_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return dict(row) if row else None


def list_meetings() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM meetings ORDER BY created_at DESC, id DESC")]


# ---------------------------------------------------------------------------
# items & breaks
# ---------------------------------------------------------------------------

def list_items(meeting_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE meeting_id = ? ORDER BY sort_order, id",
            (meeting_id,)).fetchall()
        return [dict(r) for r in rows]


def list_breaks(meeting_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM breaks WHERE meeting_id = ? ORDER BY fixed_start, id",
            (meeting_id,)).fetchall()
        return [dict(r) for r in rows]


def max_sort_order(meeting_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM items WHERE meeting_id = ?",
            (meeting_id,)).fetchone()
        return row["m"]


def create_item(meeting_id: int, title: str, planned: int, minimum: int,
                priority: int, incompressible: bool) -> dict:
    with _lock, get_conn() as conn:
        order = max_sort_order(meeting_id) + 1
        cur = conn.execute(
            """INSERT INTO items (meeting_id, title, planned_minutes, minimum_minutes,
                                  priority, incompressible, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (meeting_id, title, planned, minimum, priority,
             1 if incompressible else 0, order),
        )
        row = conn.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def get_item(item_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def delete_item(item_id: int) -> bool:
    with _lock, get_conn() as conn:
        cur = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        return cur.rowcount > 0


def move_item(meeting_id: int, item_id: int, direction: str) -> Optional[list[dict]]:
    """Swap sort_order with the previous/next item."""
    items = list_items(meeting_id)
    ids = [it["id"] for it in items]
    if item_id not in ids:
        return None
    pos = ids.index(item_id)
    swap_pos = pos - 1 if direction == "up" else pos + 1
    if not (0 <= swap_pos < len(items)):
        return items
    a, b = items[pos], items[swap_pos]
    with _lock, get_conn() as conn:
        conn.execute("UPDATE items SET sort_order = ? WHERE id = ?", (b["sort_order"], a["id"]))
        conn.execute("UPDATE items SET sort_order = ? WHERE id = ?", (a["sort_order"], b["id"]))
    return list_items(meeting_id)


# ---------------------------------------------------------------------------
# actuals (live recording) + undo log
# ---------------------------------------------------------------------------

def _snapshot(conn: sqlite3.Connection, meeting_id: int) -> str:
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM items WHERE meeting_id = ? ORDER BY sort_order, id", (meeting_id,))]
    return json.dumps(items, ensure_ascii=False)


def _push_undo(conn: sqlite3.Connection, meeting_id: int, action: str,
               snapshot: str) -> None:
    conn.execute(
        "INSERT INTO undo_log (meeting_id, action, snapshot, created_at) VALUES (?, ?, ?, ?)",
        (meeting_id, action, snapshot, datetime.now().isoformat(timespec="seconds")),
    )


def record_actual(meeting_id: int, item_id: int, actual_end: str) -> dict:
    with _lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ? AND meeting_id = ?",
                           (item_id, meeting_id)).fetchone()
        if row is None:
            raise LookupError("item not found")
        snapshot = _snapshot(conn, meeting_id)
        conn.execute("UPDATE items SET actual_end = ? WHERE id = ?",
                     (actual_end, item_id))
        _push_undo(conn, meeting_id, "actual", snapshot)
        return dict(conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())


def undo_last(meeting_id: int) -> bool:
    """Restore the item states captured by the most recent live entry."""
    with _lock, get_conn() as conn:
        log = conn.execute(
            "SELECT * FROM undo_log WHERE meeting_id = ? ORDER BY id DESC LIMIT 1",
            (meeting_id,)).fetchone()
        if log is None:
            return False
        items = json.loads(log["snapshot"])
        for it in items:
            conn.execute(
                "UPDATE items SET actual_end = ? WHERE id = ? AND meeting_id = ?",
                (it["actual_end"], it["id"], meeting_id))
            conn.execute(
                "UPDATE items SET postponed = ? WHERE id = ? AND meeting_id = ?",
                (1 if it.get("postponed") else 0, it["id"], meeting_id))
        conn.execute("DELETE FROM undo_log WHERE id = ?", (log["id"],))
        return True


def reset_actuals(meeting_id: int) -> None:
    with _lock, get_conn() as conn:
        snapshot = _snapshot(conn, meeting_id)
        _push_undo(conn, meeting_id, "reset", snapshot)
        conn.execute(
            "UPDATE items SET actual_end = NULL, postponed = 0 WHERE meeting_id = ?",
            (meeting_id,))


def postpone_items(meeting_id: int, item_ids: list[int]) -> None:
    """Atomically mark items as postponed (deferred out of this meeting).

    One undo entry is pushed for the whole batch so a single undo restores
    every item (and any earlier postponement state) at once.
    """
    if not item_ids:
        raise ValueError("no items to postpone")
    unique_ids = list(dict.fromkeys(item_ids))
    with _lock, get_conn() as conn:
        placeholders = ",".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"SELECT id FROM items WHERE meeting_id = ? AND id IN ({placeholders})",
            (meeting_id, *unique_ids)).fetchall()
        if len(rows) != len(unique_ids):
            raise LookupError("some items not found")
        snapshot = _snapshot(conn, meeting_id)
        conn.execute(
            f"UPDATE items SET postponed = 1 WHERE meeting_id = ? AND id IN ({placeholders})",
            (meeting_id, *unique_ids))
        _push_undo(conn, meeting_id, "postpone", snapshot)


def undo_count(meeting_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM undo_log WHERE meeting_id = ?",
            (meeting_id,)).fetchone()
        return row["c"]


# ---------------------------------------------------------------------------
# breaks
# ---------------------------------------------------------------------------

def create_break(meeting_id: int, title: str, fixed_start: str, duration: int) -> dict:
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO breaks (meeting_id, title, fixed_start, duration_minutes) VALUES (?, ?, ?, ?)",
            (meeting_id, title or "休息", fixed_start, duration))
        return dict(conn.execute("SELECT * FROM breaks WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())


def get_break(break_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM breaks WHERE id = ?", (break_id,)).fetchone()
        return dict(row) if row else None


def delete_break(break_id: int) -> bool:
    with _lock, get_conn() as conn:
        cur = conn.execute("DELETE FROM breaks WHERE id = ?", (break_id,))
        return cur.rowcount > 0
