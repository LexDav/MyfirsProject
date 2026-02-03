import sqlite3
from datetime import datetime
from typing import Iterable


def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE,
            username TEXT,
            registered_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            mode TEXT,
            description TEXT,
            result TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_user(db_path: str, chat_id: int, username: str | None) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE chat_id = ?", (chat_id,))
    existing = cur.fetchone()
    if existing is None:
        cur.execute(
            "INSERT INTO users (chat_id, username, registered_at) VALUES (?, ?, ?)",
            (chat_id, username, datetime.utcnow().isoformat()),
        )
    conn.commit()
    conn.close()


def save_query(db_path: str, chat_id: int, mode: str, description: str, result: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO queries (chat_id, mode, description, result, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, mode, description, result, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_history(db_path: str, chat_id: int, limit: int = 10) -> Iterable[tuple]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT created_at, mode, description, result FROM queries WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows
