"""Persistent, encrypted notes store for Voice Memory Assistant v2.0.

Backed by SQLite with SQLCipher (AES-256) encryption at rest. The encryption key
and DB path come from the environment (SQLCIPHER_KEY, NOTES_DB_PATH) — never
hardcoded. The DB file lives next to the backend on the local machine.

Schema (v2.0):  notes(id, text, tags, created_at, updated_at)

Schema (v3.0, additive — the v2.0 ``notes`` table is kept untouched):
    note_docs(id, title, description, created_at, updated_at)
    note_entries(id, doc_id, text, created_at)   # a doc holds an arbitrary-length
                                                  # list of entries

A note "doc" is a titled, described container; entries are the individual lines the
assistant jots down inside it. ``migrate_legacy_notes`` folds any pre-existing v2.0
flat notes into a single "Imported Notes" doc without dropping the old table.

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


@dataclass
class NoteEntry:
    """A single jotted-down line inside a note doc. Text may be any length."""

    id: str
    doc_id: str
    text: str
    created_at: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NoteDoc:
    """A titled, described container that holds a list of entries.

    ``entries`` is populated by the "full" reads (e.g. get_doc_full); the
    lightweight docs-list reads leave it empty so the navigator agent never
    sees entry contents.
    """

    id: str
    title: str
    description: str
    created_at: float
    updated_at: float
    entries: list[NoteEntry] | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # Drop the entries key when not loaded so the docs-list payload stays lean.
        if self.entries is None:
            d.pop("entries", None)
        return d


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
            # --- v2.0 flat notes (unchanged, kept for backward compatibility) ---
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
            # --- v3.0 note docs + entries (additive) ---
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS note_docs (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS note_entries (
                    id          TEXT PRIMARY KEY,
                    doc_id      TEXT NOT NULL,
                    text        TEXT NOT NULL,
                    created_at  REAL NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_doc ON note_entries(doc_id)"
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

    # ===================================================================
    # v3.0 — note docs (containers) CRUD
    # ===================================================================

    def create_doc(self, title: str, description: str = "") -> NoteDoc:
        ts = _now()
        doc = NoteDoc(
            id=_new_id(), title=title.strip(), description=description.strip(),
            created_at=ts, updated_at=ts, entries=[],
        )
        with self._lock, self._conn() as con:
            con.execute(
                "INSERT INTO note_docs (id, title, description, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (doc.id, doc.title, doc.description, doc.created_at, doc.updated_at),
            )
        return doc

    def get_doc(self, doc_id: str) -> NoteDoc | None:
        """Fetch a doc's metadata only (no entries loaded)."""
        with self._lock, self._conn() as con:
            row = con.execute(
                "SELECT * FROM note_docs WHERE id = ?", (doc_id,)
            ).fetchone()
        return _row_to_doc(row) if row else None

    def get_doc_full(self, doc_id: str) -> NoteDoc | None:
        """Fetch a doc WITH its entries — the only context a NoteAgent should load."""
        with self._lock, self._conn() as con:
            row = con.execute(
                "SELECT * FROM note_docs WHERE id = ?", (doc_id,)
            ).fetchone()
            if not row:
                return None
            erows = con.execute(
                "SELECT * FROM note_entries WHERE doc_id = ? ORDER BY created_at ASC",
                (doc_id,),
            ).fetchall()
        doc = _row_to_doc(row)
        doc.entries = [_row_to_entry(e) for e in erows]
        return doc

    def update_doc(
        self, doc_id: str, title: str | None = None, description: str | None = None
    ) -> NoteDoc | None:
        with self._lock, self._conn() as con:
            row = con.execute(
                "SELECT * FROM note_docs WHERE id = ?", (doc_id,)
            ).fetchone()
            if not row:
                return None
            new_title = title.strip() if title is not None else row["title"]
            new_desc = description.strip() if description is not None else row["description"]
            ts = _now()
            con.execute(
                "UPDATE note_docs SET title = ?, description = ?, updated_at = ? WHERE id = ?",
                (new_title, new_desc, ts, doc_id),
            )
            updated = con.execute(
                "SELECT * FROM note_docs WHERE id = ?", (doc_id,)
            ).fetchone()
        return _row_to_doc(updated)

    def delete_doc(self, doc_id: str) -> bool:
        """Delete a doc and all of its entries (explicit cascade)."""
        with self._lock, self._conn() as con:
            con.execute("DELETE FROM note_entries WHERE doc_id = ?", (doc_id,))
            cur = con.execute("DELETE FROM note_docs WHERE id = ?", (doc_id,))
            return cur.rowcount > 0

    def list_docs(self) -> list[NoteDoc]:
        """List every doc's metadata (id, title, description) — NO entries.

        This is the navigator agent's entire context: it must never see entry text.
        """
        with self._lock, self._conn() as con:
            rows = con.execute(
                "SELECT * FROM note_docs ORDER BY updated_at DESC"
            ).fetchall()
        return [_row_to_doc(r) for r in rows]

    def search_docs(self, query: str) -> list[NoteDoc]:
        """Find docs by title or description (for open_note resolution)."""
        q = f"%{query.strip()}%"
        with self._lock, self._conn() as con:
            rows = con.execute(
                "SELECT * FROM note_docs WHERE title LIKE ? OR description LIKE ? "
                "ORDER BY updated_at DESC",
                (q, q),
            ).fetchall()
        return [_row_to_doc(r) for r in rows]

    # ===================================================================
    # v3.0 — entries (scoped to a doc) CRUD
    # ===================================================================

    def _touch_doc(self, con, doc_id: str) -> None:
        con.execute("UPDATE note_docs SET updated_at = ? WHERE id = ?", (_now(), doc_id))

    def add_entry(self, doc_id: str, text: str) -> NoteEntry | None:
        with self._lock, self._conn() as con:
            exists = con.execute(
                "SELECT 1 FROM note_docs WHERE id = ?", (doc_id,)
            ).fetchone()
            if not exists:
                return None
            entry = NoteEntry(
                id=_new_id(), doc_id=doc_id, text=text.strip(), created_at=_now()
            )
            con.execute(
                "INSERT INTO note_entries (id, doc_id, text, created_at) VALUES (?,?,?,?)",
                (entry.id, entry.doc_id, entry.text, entry.created_at),
            )
            self._touch_doc(con, doc_id)
        return entry

    def get_entry(self, entry_id: str) -> NoteEntry | None:
        with self._lock, self._conn() as con:
            row = con.execute(
                "SELECT * FROM note_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return _row_to_entry(row) if row else None

    def update_entry(self, entry_id: str, text: str) -> NoteEntry | None:
        with self._lock, self._conn() as con:
            row = con.execute(
                "SELECT * FROM note_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if not row:
                return None
            con.execute(
                "UPDATE note_entries SET text = ? WHERE id = ?", (text.strip(), entry_id)
            )
            self._touch_doc(con, row["doc_id"])
            updated = con.execute(
                "SELECT * FROM note_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return _row_to_entry(updated)

    def delete_entry(self, entry_id: str) -> bool:
        with self._lock, self._conn() as con:
            row = con.execute(
                "SELECT doc_id FROM note_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if not row:
                return False
            con.execute("DELETE FROM note_entries WHERE id = ?", (entry_id,))
            self._touch_doc(con, row["doc_id"])
            return True

    def list_entries(self, doc_id: str) -> list[NoteEntry]:
        with self._lock, self._conn() as con:
            rows = con.execute(
                "SELECT * FROM note_entries WHERE doc_id = ? ORDER BY created_at ASC",
                (doc_id,),
            ).fetchall()
        return [_row_to_entry(r) for r in rows]

    # ===================================================================
    # v3.0 — migration from v2.0 flat notes
    # ===================================================================

    def migrate_legacy_notes(self, title: str = "Imported Notes") -> NoteDoc | None:
        """Fold any pre-existing v2.0 flat notes into a single doc.

        Idempotent and non-destructive: only runs when there are no docs yet and
        the old ``notes`` table has rows. The old table is left intact.
        Returns the created doc, or None if there was nothing to migrate.
        """
        with self._lock, self._conn() as con:
            doc_count = con.execute("SELECT COUNT(*) AS c FROM note_docs").fetchone()["c"]
            if doc_count:
                return None
            old = con.execute(
                "SELECT * FROM notes ORDER BY created_at ASC"
            ).fetchall()
            if not old:
                return None
            ts = _now()
            doc_id = _new_id()
            con.execute(
                "INSERT INTO note_docs (id, title, description, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (doc_id, title, "Migrated from your earlier saved notes.", ts, ts),
            )
            for n in old:
                con.execute(
                    "INSERT INTO note_entries (id, doc_id, text, created_at) VALUES (?,?,?,?)",
                    (_new_id(), doc_id, n["text"], n["created_at"]),
                )
        return self.get_doc_full(doc_id)


def _row_to_doc(row) -> NoteDoc:
    return NoteDoc(
        id=row["id"], title=row["title"], description=row["description"],
        created_at=row["created_at"], updated_at=row["updated_at"], entries=None,
    )


def _row_to_entry(row) -> NoteEntry:
    return NoteEntry(
        id=row["id"], doc_id=row["doc_id"], text=row["text"], created_at=row["created_at"],
    )


def _row_to_note(row) -> Note:
    return Note(
        id=row["id"], text=row["text"], tags=row["tags"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )
