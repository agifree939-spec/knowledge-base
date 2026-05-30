import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Optional
from app.config import DB_PATH


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize database schema with FTS5 full-text search."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT UNIQUE NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'article',
            title       TEXT DEFAULT '',
            summary     TEXT DEFAULT '',
            tags        TEXT DEFAULT '[]',
            full_text   TEXT DEFAULT '',
            source_author TEXT DEFAULT '',
            source_date TEXT DEFAULT '',
            captured_at TEXT NOT NULL,
            status      TEXT DEFAULT 'done',
            error_message TEXT DEFAULT ''
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
            title, summary, tags, full_text,
            content='entries',
            content_rowid='id',
            tokenize='unicode61'
        );

        CREATE TABLE IF NOT EXISTS images (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id    INTEGER NOT NULL,
            filename    TEXT NOT NULL,
            local_path  TEXT NOT NULL,
            original_url TEXT DEFAULT '',
            FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
        );

        -- Triggers to keep FTS in sync
        CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
            INSERT INTO entries_fts(rowid, title, summary, tags, full_text)
            VALUES (new.id, new.title, new.summary, new.tags, new.full_text);
        END;

        CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
            INSERT INTO entries_fts(entries_fts, rowid, title, summary, tags, full_text)
            VALUES ('delete', old.id, old.title, old.summary, old.tags, old.full_text);
        END;

        CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
            INSERT INTO entries_fts(entries_fts, rowid, title, summary, tags, full_text)
            VALUES ('delete', old.id, old.title, old.summary, old.tags, old.full_text);
            INSERT INTO entries_fts(rowid, title, summary, tags, full_text)
            VALUES (new.id, new.title, new.summary, new.tags, new.full_text);
        END;
    """)
    conn.commit()
    conn.close()


def insert_entry(
    url: str,
    content_type: str,
    title: str,
    summary: str,
    tags: list,
    full_text: str,
    source_author: str = "",
    source_date: str = "",
    status: str = "done",
    error_message: str = "",
) -> int:
    conn = get_db()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO entries
           (url, content_type, title, summary, tags, full_text,
            source_author, source_date, captured_at, status, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            url, content_type, title, summary, json.dumps(tags, ensure_ascii=False),
            full_text, source_author, source_date,
            datetime.utcnow().isoformat(), status, error_message,
        ),
    )
    entry_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return entry_id


def insert_image(entry_id: int, filename: str, local_path: str, original_url: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT INTO images (entry_id, filename, local_path, original_url) VALUES (?, ?, ?, ?)",
        (entry_id, filename, local_path, original_url),
    )
    conn.commit()
    conn.close()


def get_entry(entry_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        conn.close()
        return None
    entry = dict(row)
    entry["tags"] = json.loads(entry.get("tags", "[]"))
    images = conn.execute(
        "SELECT * FROM images WHERE entry_id = ?", (entry_id,)
    ).fetchall()
    entry["images"] = [dict(img) for img in images]
    conn.close()
    return entry


def get_entry_by_url(url: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM entries WHERE url = ?", (url,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def search_entries(query: str, limit: int = 10, offset: int = 0) -> list:
    conn = get_db()
    rows = conn.execute(
        """SELECT e.*, rank
           FROM entries_fts fts
           JOIN entries e ON e.id = fts.rowid
           WHERE entries_fts MATCH ?
           ORDER BY rank
           LIMIT ? OFFSET ?""",
        (query, limit, offset),
    ).fetchall()
    results = []
    for row in rows:
        entry = dict(row)
        entry["tags"] = json.loads(entry.get("tags", "[]"))
        results.append(entry)
    conn.close()
    return results


def list_entries(
    content_type: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list:
    conn = get_db()
    if content_type:
        rows = conn.execute(
            "SELECT * FROM entries WHERE content_type = ? AND status = 'done' ORDER BY captured_at DESC LIMIT ? OFFSET ?",
            (content_type, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM entries WHERE status = 'done' ORDER BY captured_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    results = []
    for row in rows:
        entry = dict(row)
        entry["tags"] = json.loads(entry.get("tags", "[]"))
        results.append(entry)
    conn.close()
    return results


def delete_entry(entry_id: int) -> bool:
    conn = get_db()
    cursor = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_stats() -> dict:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM entries WHERE status='done'").fetchone()[0]
    tweets = conn.execute("SELECT COUNT(*) FROM entries WHERE content_type='tweet' AND status='done'").fetchone()[0]
    articles = conn.execute("SELECT COUNT(*) FROM entries WHERE content_type='article' AND status='done'").fetchone()[0]
    images = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    conn.close()
    return {"total": total, "tweets": tweets, "articles": articles, "images": images}
