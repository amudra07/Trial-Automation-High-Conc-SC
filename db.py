"""
db.py

SQLite-backed storage for the SC Tech Tracker. Replaces the static
ENTRIES list in tech_landscape_data.py with a real table that supports:

  - status: 'curated' (shown on the dashboard) vs 'staging' (awaiting
    human review after a Claude search pass)
  - confidence: 0-1, set by the extraction step
  - concentration_type: 'achieved_clinical' | 'achieved_preclinical' |
    'target' | 'theoretical' | None -- critical so the dashboard never
    conflates a formulation-study number with a clinically-demonstrated one
  - entry_history: append-only log of every field change, so you can see
    a competitor's concentration trajectory over time rather than only
    the latest number

Uses SQLite for zero-setup local use. If you outgrow a single laptop,
swap DB_PATH for a Postgres connection string via SQLAlchemy later --
the function signatures below are written so that swap doesn't ripple
into app.py.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "sc_tracker.db"

FIELDS = [
    "id", "name", "developer", "category", "phase",
    "concentration_mgml", "concentration_type", "concentration_display",
    "needle_size_g", "mechanism_short", "mechanism_long",
    "deals", "source_name", "source_url", "is_internal",
    "status", "confidence",
]


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            developer TEXT,
            category TEXT NOT NULL,
            phase TEXT NOT NULL,
            concentration_mgml REAL,
            concentration_type TEXT,
            concentration_display TEXT,
            needle_size_g TEXT,
            mechanism_short TEXT,
            mechanism_long TEXT,
            deals TEXT,               -- JSON-encoded list[str]
            source_name TEXT,
            source_url TEXT,
            is_internal INTEGER DEFAULT 0,
            status TEXT DEFAULT 'curated',   -- 'curated' | 'staging'
            confidence REAL,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entry_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT,
            field TEXT,
            old_value TEXT,
            new_value TEXT,
            changed_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def _row_to_entry(row) -> dict:
    e = dict(row)
    e["deals"] = json.loads(e["deals"]) if e["deals"] else []
    e["is_internal"] = bool(e["is_internal"])
    return e


def seed_from_static(static_entries: list[dict]):
    """One-time migration from tech_landscape_data.ENTRIES. Safe to
    call repeatedly -- uses INSERT OR IGNORE so it won't clobber rows
    a human has since edited or approved."""
    conn = _connect()
    now = datetime.now(timezone.utc).isoformat()
    for e in static_entries:
        conn.execute(
            """INSERT OR IGNORE INTO entries
               (id, name, developer, category, phase, concentration_mgml,
                concentration_type, concentration_display, needle_size_g,
                mechanism_short, mechanism_long, deals, source_name,
                source_url, is_internal, status, confidence,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                e["id"], e["name"], e["developer"], e["category"], e["phase"],
                e.get("concentration_mgml"),
                e.get("concentration_type", "achieved_preclinical" if e.get("concentration_mgml") else None),
                e.get("concentration_display", ""),
                e.get("needle_size_g", "Not disclosed"),
                e.get("mechanism_short", ""), e.get("mechanism_long", ""),
                json.dumps(e.get("deals", [])),
                e.get("source_name", ""), e.get("source_url"),
                int(e.get("is_internal", False)),
                "curated", 1.0, now, now,
            ),
        )
    conn.commit()
    conn.close()


def get_entries(status: str = "curated") -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM entries WHERE status = ? ORDER BY updated_at DESC", (status,)
    ).fetchall()
    conn.close()
    return [_row_to_entry(r) for r in rows]


def get_entry(entry_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    return _row_to_entry(row) if row else None


def entries_with_concentration(status: str = "curated") -> list[dict]:
    return [e for e in get_entries(status) if e["concentration_mgml"] is not None]


def entries_by_category(category: str, status: str = "curated") -> list[dict]:
    return [e for e in get_entries(status) if e["category"] == category]


def add_staging_entries(candidates: list[dict]):
    """Insert Claude's extracted candidates as 'staging' rows awaiting
    human review. Uses id + '::candidate::N' so a re-search doesn't
    collide with an already-curated id of the same asset."""
    conn = _connect()
    now = datetime.now(timezone.utc).isoformat()
    for i, e in enumerate(candidates):
        staging_id = f"{e.get('id', 'candidate')}::staging::{now[:19]}::{i}"
        conn.execute(
            """INSERT INTO entries
               (id, name, developer, category, phase, concentration_mgml,
                concentration_type, concentration_display, needle_size_g,
                mechanism_short, mechanism_long, deals, source_name,
                source_url, is_internal, status, confidence,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                staging_id, e.get("name", "Unknown"), e.get("developer", "Unknown"),
                e.get("category", "Suspension / particle"), e.get("phase", "Preclinical"),
                e.get("concentration_mgml"), e.get("concentration_type"),
                e.get("concentration_display", ""), e.get("needle_size_g", "Not disclosed"),
                e.get("mechanism_short", ""), e.get("mechanism_long", ""),
                json.dumps(e.get("deals", [])),
                e.get("source_name", ""), e.get("source_url"),
                int(e.get("is_internal", False)),
                "staging", e.get("confidence", 0.5),
                now, now,
            ),
        )
    conn.commit()
    conn.close()


def approve_entry(staging_id: str, promote_as_id: str | None = None):
    """Move a staging row to curated. If promote_as_id matches an
    existing curated entry (same asset, updated numbers), log the
    diff to entry_history before overwriting."""
    conn = _connect()
    staged = conn.execute("SELECT * FROM entries WHERE id = ?", (staging_id,)).fetchone()
    if not staged:
        conn.close()
        return
    staged = dict(staged)
    final_id = promote_as_id or staged["id"].split("::staging::")[0]
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute("SELECT * FROM entries WHERE id = ? AND status = 'curated'", (final_id,)).fetchone()
    if existing:
        existing = dict(existing)
        for field in ["concentration_mgml", "phase", "concentration_type"]:
            if str(existing.get(field)) != str(staged.get(field)):
                conn.execute(
                    "INSERT INTO entry_history (entry_id, field, old_value, new_value, changed_at) VALUES (?,?,?,?,?)",
                    (final_id, field, str(existing.get(field)), str(staged.get(field)), now),
                )
        conn.execute("DELETE FROM entries WHERE id = ?", (final_id,))

    conn.execute(
        """INSERT INTO entries
           (id, name, developer, category, phase, concentration_mgml,
            concentration_type, concentration_display, needle_size_g,
            mechanism_short, mechanism_long, deals, source_name,
            source_url, is_internal, status, confidence, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            final_id, staged["name"], staged["developer"], staged["category"], staged["phase"],
            staged["concentration_mgml"], staged["concentration_type"], staged["concentration_display"],
            staged["needle_size_g"], staged["mechanism_short"], staged["mechanism_long"],
            staged["deals"], staged["source_name"], staged["source_url"], staged["is_internal"],
            "curated", staged["confidence"], now, now,
        ),
    )
    conn.execute("DELETE FROM entries WHERE id = ? AND status = 'staging'", (staging_id,))
    conn.commit()
    conn.close()


def reject_entry(staging_id: str):
    conn = _connect()
    conn.execute("DELETE FROM entries WHERE id = ?", (staging_id,))
    conn.commit()
    conn.close()


def update_staging_entry(staging_id: str, updates: dict):
    """Let a human edit fields before approving (e.g. fix a stage the
    model got wrong)."""
    if not updates:
        return
    conn = _connect()
    cols = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [staging_id]
    conn.execute(f"UPDATE entries SET {cols} WHERE id = ?", values)
    conn.commit()
    conn.close()


def get_history(entry_id: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM entry_history WHERE entry_id = ? ORDER BY changed_at DESC", (entry_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
