"""Persistent, encrypted notes store for Voice Memory Assistant v2.0.

Backed by SQLite with SQLCipher (AES-256) encryption at rest. The encryption key
and DB path come from the environment (SQLCIPHER_KEY, NOTES_DB_PATH) — never
hardcoded. The DB file lives next to the backend on the local machine.

Schema:  notes(id, text, tags, created_at, updated_at)

Notes persist across conversations (the v2.0 reversal of v1.0's RAM-only memory).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlcipher


@dataclass
class Note:
    id: str
    text: str
    tags: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> float:
    return round(time.time(), 3)


def _new_id() -> str:
    # short, voice-friendly id
    return uuid.uuid4().hex[:8]


class NotesStore:
    """Thread-safe (via a global lock) SQLCipher-backed notes store.

    A fresh connection is opened per operation and keyed with PRAGMA key, which
    keeps things simple and safe across the agent's event loop + data handlers.
    """

    def __init__(self, db_path: str, key: str) -> None:
        if not key:
            raise ValueError("SQLCIPHER_KEY is empty — refusing to open an unencrypted store.")
        self.db_path = str(Path(db_path).expanduser())
        self._key = key
        self._lock = threading.Lock()
        self._init_schema()

    @classmethod
    def from_env(cls) -> "NotesStore":
        return cls(
            db_path=os.getenv("NOTES_DB_PATH", "notes.db"),
            key=os.getenv("SQLCIPHER_KEY", ""),
        )

    @contextmanager
    def _conn(self):
        con = sqlcipher.connect(self.db_path)
        try:
            # PRAGMA key cannot be parameterized; escape single quotes defensively.
            escaped = self._key.replace("'", "''")
            con.execute(f"PRAGMA key = '{escaped}'")
            con.row_factory = sqlcipher.Row
            yield con
            con.commit()
        finally:
            con.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id          TEXT PRIMARY KEY,
                    text        TEXT NOT NULL,
                    tags        TEXT NOT NULL DEFAULT '',
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL
                )
                """
            )

    # ---- CRUD -------------------------------------------------------------

    def create(self, text: str, tags: str = "") -> Note:
        note = Note(id=_new_id(), text=text.strip(), tags=tags.strip(),
                    created_at=_now(), updated_at=_now())
        with self._lock, self._conn() as con:
            con.execute(
                "INSERT INTO notes (id, text, tags, created_at, updated_at) VALUES (?,?,?,?,?)",
                (note.id, note.text, note.tags, note.created_at, note.updated_at),
            )
        return note

    def get(self, note_id: str) -> Note | None:
        with self._lock, self._conn() as con:
            row = con.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return _row_to_note(row) if row else None

    def update(self, note_id: str, text: str | None = None,
               tags: str | None = None) -> Note | None:
        with self._lock, self._conn() as con:
            row = con.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if not row:
                return None
            new_text = text.strip() if text is not None else row["text"]
            new_tags = tags.strip() if tags is not None else row["tags"]
            ts = _now()
            con.execute(
                "UPDATE notes SET text = ?, tags = ?, updated_at = ? WHERE id = ?",
                (new_text, new_tags, ts, note_id),
            )
            updated = con.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return _row_to_note(updated)

    def delete(self, note_id: str) -> bool:
        with self._lock, self._conn() as con:
            cur = con.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            return cur.rowcount > 0

    def delete_all(self) -> int:
        with self._lock, self._conn() as con:
            cur = con.execute("DELETE FROM notes")
            return cur.rowcount

    def list_all(self) -> list[Note]:
        with self._lock, self._conn() as con:
            rows = con.execute("SELECT * FROM notes ORDER BY created_at ASC").fetchall()
        return [_row_to_note(r) for r in rows]

    def search(self, query: str) -> list[Note]:
        q = f"%{query.strip()}%"
        with self._lock, self._conn() as con:
            rows = con.execute(
                "SELECT * FROM notes WHERE text LIKE ? OR tags LIKE ? ORDER BY created_at ASC",
                (q, q),
            ).fetchall()
        return [_row_to_note(r) for r in rows]


def _row_to_note(row) -> Note:
    return Note(
        id=row["id"], text=row["text"], tags=row["tags"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )
