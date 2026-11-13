"""
Persistent memory across sleep/wake sessions and script restarts, stored in
a local SQLite database (config.MEMORY_DB_PATH).

Two kinds of memory:
  - facts: things explicitly told to remember ("remember I like jazz"),
    kept small and always visible to the LLM in its system prompt
  - sessions: full transcripts of every past conversation, not injected
    into every prompt (would blow the context window) but searchable on
    demand via the recall tool ("what did we talk about yesterday")
"""

import logging
import sqlite3
from datetime import datetime

import config

logger = logging.getLogger("voice_assistant")


def _connect():
    conn = sqlite3.connect(config.MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def connect():
    """Public connection to the memory database (row_factory = sqlite3.Row). Feature modules
    (reminders, notes, briefing state) keep their own tables in the same file: they create
    them lazily with CREATE TABLE IF NOT EXISTS and close the connection when done."""
    return _connect()


def init_db():
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration for DBs created before the 'key' column existed - lets us
    # look up facts like "favorite musician" deterministically by an exact
    # key instead of relying on the LLM to notice/use them.
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(facts)")]
    if "key" not in cols:
        conn.execute("ALTER TABLE facts ADD COLUMN key TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            transcript TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()
    conn.close()
    logger.info("Memory database ready at %s", config.MEMORY_DB_PATH)


# --- Facts --------------------------------------------------------------

def add_fact(content: str, key: str = None):
    conn = _connect()
    if key:
        # A keyed fact replaces any previous one with the same key, so
        # asking "what's my favorite musician" again after changing your
        # mind updates the answer instead of leaving two contradictory
        # facts sitting in the list.
        conn.execute("DELETE FROM facts WHERE key = ?", (key,))
    conn.execute(
        "INSERT INTO facts (content, created_at, key) VALUES (?, ?, ?)",
        (content.strip(), datetime.now().isoformat(), key),
    )
    conn.commit()
    conn.close()


def get_fact_by_key(key: str):
    """Exact-match lookup for a keyed fact, e.g. key='favorite_musician'. Returns None if not set."""
    conn = _connect()
    row = conn.execute(
        "SELECT content FROM facts WHERE key = ? ORDER BY id DESC LIMIT 1", (key,)
    ).fetchone()
    conn.close()
    return row["content"] if row else None


def all_facts() -> dict:
    """
    {key: content} for every KEYED fact (e.g. 'favorite_musician' -> "User's
    favorite musician is Arijit Singh"). initiative.py looks for exactly this
    name to build its "memory follow-up" lines; without it that trigger - the
    highest-weighted one - could never fire.
    """
    conn = _connect()
    rows = conn.execute(
        "SELECT key, content FROM facts WHERE key IS NOT NULL ORDER BY id"
    ).fetchall()
    conn.close()
    return {r["key"]: r["content"] for r in rows}


def get_recent_facts(limit: int = 15):
    conn = _connect()
    rows = conn.execute(
        "SELECT content FROM facts ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [r["content"] for r in reversed(rows)]


def forget_facts_matching(keyword: str):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, content FROM facts WHERE content LIKE ?", (f"%{keyword}%",)
    ).fetchall()
    ids = [r["id"] for r in rows]
    removed = [r["content"] for r in rows]
    if ids:
        conn.executemany("DELETE FROM facts WHERE id = ?", [(i,) for i in ids])
        conn.commit()
    conn.close()
    return removed


# --- Sessions -------------------------------------------------------------

def start_session() -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO sessions (started_at, transcript) VALUES (?, '')",
        (datetime.now().isoformat(),),
    )
    conn.commit()
    session_id = cur.lastrowid
    conn.close()
    return session_id


def append_to_session(session_id: int, role: str, text: str):
    if session_id is None:
        return
    conn = _connect()
    row = conn.execute(
        "SELECT transcript FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    existing = row["transcript"] if row else ""
    conn.execute(
        "UPDATE sessions SET transcript = ? WHERE id = ?",
        (existing + f"{role}: {text}\n", session_id),
    )
    conn.commit()
    conn.close()


def end_session(session_id: int):
    if session_id is None:
        return
    conn = _connect()
    conn.execute(
        "UPDATE sessions SET ended_at = ? WHERE id = ?",
        (datetime.now().isoformat(), session_id),
    )
    conn.commit()
    conn.close()


def search_sessions(query: str, limit: int = 2, exclude_session_id=None):
    conn = _connect()
    sql = "SELECT started_at, transcript FROM sessions WHERE transcript LIKE ?"
    params = [f"%{query}%"]
    if exclude_session_id is not None:
        sql += " AND id != ?"
        params.append(exclude_session_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [{"started_at": r["started_at"], "transcript": r["transcript"]} for r in rows]
