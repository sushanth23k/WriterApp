"""PostgreSQL-backed, per-user notes store for DropNote (writer_app schema).

This replaces the old local SQLCipher store (store.py). All note data now lives in
Postgres (Supabase) under the ``writer_app`` schema and is scoped to the owning user
by email, so every account sees ONLY its own notes:

    writer_app.note_docs(id, user_email, title, description, created_at, updated_at)
    writer_app.note_entries(id, doc_id, text, created_at)   -- doc_id cascades on delete

A note "doc" is a titled, described container owned by one user; entries are the
individual lines jotted inside it. Encryption at rest is handled by Postgres/Supabase
(no app-level SQLCipher layer anymore).

The connection string comes from ``DATABASE_URL`` (same database as the auth
``user_schema`` store); nothing is hardcoded. Timestamps are stored as epoch floats to
keep the JSON contract identical to what the app already expects.

Every public method takes the owning ``user_email`` and scopes its query to it. For
the agent — which operates on behalf of a single signed-in user — bind an email once
with :meth:`NotesStore.for_user` to get a :class:`UserNotesStore` whose method names
mirror the old store (``create_doc``, ``add_entry`` …) but need no email argument.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass

import psycopg

import env_util

SCHEMA = "writer_app"
DOCS = f"{SCHEMA}.note_docs"
ENTRIES = f"{SCHEMA}.note_entries"


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
    lightweight docs-list reads leave it None so the docs-list payload never
    carries entry contents (the navigator agent must not see them).
    """

    id: str
    title: str
    description: str
    created_at: float
    updated_at: float
    entries: list[NoteEntry] | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.entries is None:
            d.pop("entries", None)
        return d


def _now() -> float:
    return round(time.time(), 3)


def _new_id() -> str:
    # short, voice-friendly id
    return uuid.uuid4().hex[:8]


def _norm(email: str) -> str:
    return (email or "").strip().lower()


class NotesStore:
    """Postgres-backed notes store. Every method is scoped to a ``user_email``.

    A fresh connection is opened per operation (psycopg manages short-lived
    connections cleanly), mirroring the per-op pattern used by ``user_store.py``.
    """

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("DATABASE_URL is empty — cannot open the notes store.")
        self._dsn = dsn
        self._init_schema()

    @classmethod
    def from_env(cls) -> "NotesStore":
        return cls(dsn=env_util.get("DATABASE_URL"))

    def for_user(self, email: str) -> "UserNotesStore":
        """Bind a user so the agent can call methods without repeating the email."""
        return UserNotesStore(self, _norm(email))

    @contextmanager
    def _conn(self):
        con = psycopg.connect(self._dsn)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init_schema(self) -> None:
        with self._conn() as con:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            con.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {DOCS} (
                    id          TEXT PRIMARY KEY,
                    user_email  TEXT NOT NULL,
                    title       TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    created_at  DOUBLE PRECISION NOT NULL,
                    updated_at  DOUBLE PRECISION NOT NULL
                )
                """
            )
            # Migrate a note_docs table that predates per-user scoping: add the owner
            # column (nullable here so existing rows survive; a backfill assigns them).
            con.execute(
                f"ALTER TABLE {DOCS} ADD COLUMN IF NOT EXISTS user_email TEXT"
            )
            con.execute(
                f"CREATE INDEX IF NOT EXISTS idx_note_docs_user ON {DOCS}(user_email)"
            )
            con.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {ENTRIES} (
                    id          TEXT PRIMARY KEY,
                    doc_id      TEXT NOT NULL
                                REFERENCES {DOCS}(id) ON DELETE CASCADE,
                    text        TEXT NOT NULL,
                    created_at  DOUBLE PRECISION NOT NULL
                )
                """
            )
            con.execute(
                f"CREATE INDEX IF NOT EXISTS idx_note_entries_doc ON {ENTRIES}(doc_id)"
            )

    # ---- docs (containers) CRUD -------------------------------------------

    def create_doc(self, user_email: str, title: str, description: str = "") -> NoteDoc:
        email = _norm(user_email)
        ts = _now()
        doc = NoteDoc(
            id=_new_id(), title=title.strip(), description=description.strip(),
            created_at=ts, updated_at=ts, entries=[],
        )
        with self._conn() as con:
            con.execute(
                f"INSERT INTO {DOCS} "
                "(id, user_email, title, description, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (doc.id, email, doc.title, doc.description, doc.created_at, doc.updated_at),
            )
        return doc

    def get_doc(self, user_email: str, doc_id: str) -> NoteDoc | None:
        """Fetch a doc's metadata only (no entries loaded), scoped to the user."""
        with self._conn() as con:
            row = con.execute(
                f"SELECT id, title, description, created_at, updated_at FROM {DOCS} "
                "WHERE id = %s AND user_email = %s",
                (doc_id, _norm(user_email)),
            ).fetchone()
        return _row_to_doc(row) if row else None

    def get_doc_full(self, user_email: str, doc_id: str) -> NoteDoc | None:
        """Fetch a doc WITH its entries — the only context a NoteAgent should load."""
        email = _norm(user_email)
        with self._conn() as con:
            row = con.execute(
                f"SELECT id, title, description, created_at, updated_at FROM {DOCS} "
                "WHERE id = %s AND user_email = %s",
                (doc_id, email),
            ).fetchone()
            if not row:
                return None
            erows = con.execute(
                f"SELECT id, doc_id, text, created_at FROM {ENTRIES} "
                "WHERE doc_id = %s ORDER BY created_at ASC",
                (doc_id,),
            ).fetchall()
        doc = _row_to_doc(row)
        doc.entries = [_row_to_entry(e) for e in erows]
        return doc

    def update_doc(
        self, user_email: str, doc_id: str,
        title: str | None = None, description: str | None = None,
    ) -> NoteDoc | None:
        email = _norm(user_email)
        with self._conn() as con:
            row = con.execute(
                f"SELECT title, description FROM {DOCS} "
                "WHERE id = %s AND user_email = %s",
                (doc_id, email),
            ).fetchone()
            if not row:
                return None
            new_title = title.strip() if title is not None else row[0]
            new_desc = description.strip() if description is not None else row[1]
            con.execute(
                f"UPDATE {DOCS} SET title = %s, description = %s, updated_at = %s "
                "WHERE id = %s AND user_email = %s",
                (new_title, new_desc, _now(), doc_id, email),
            )
        return self.get_doc(email, doc_id)

    def delete_doc(self, user_email: str, doc_id: str) -> bool:
        """Delete a doc and all of its entries.

        Entries are removed explicitly (not relying on a DB-level ON DELETE CASCADE),
        so this works the same on a freshly-created table and on a pre-existing one
        migrated from the single-user era that lacks the foreign key.
        """
        email = _norm(user_email)
        with self._conn() as con:
            owned = con.execute(
                f"SELECT 1 FROM {DOCS} WHERE id = %s AND user_email = %s",
                (doc_id, email),
            ).fetchone()
            if not owned:
                return False
            con.execute(f"DELETE FROM {ENTRIES} WHERE doc_id = %s", (doc_id,))
            con.execute(
                f"DELETE FROM {DOCS} WHERE id = %s AND user_email = %s", (doc_id, email)
            )
            return True

    def list_docs(self, user_email: str) -> list[NoteDoc]:
        """List this user's docs (id, title, description) — NO entries."""
        with self._conn() as con:
            rows = con.execute(
                f"SELECT id, title, description, created_at, updated_at FROM {DOCS} "
                "WHERE user_email = %s ORDER BY updated_at DESC",
                (_norm(user_email),),
            ).fetchall()
        return [_row_to_doc(r) for r in rows]

    def search_docs(self, user_email: str, query: str) -> list[NoteDoc]:
        """Find this user's docs by title or description (for open_note resolution)."""
        q = f"%{query.strip()}%"
        with self._conn() as con:
            rows = con.execute(
                f"SELECT id, title, description, created_at, updated_at FROM {DOCS} "
                "WHERE user_email = %s AND (title ILIKE %s OR description ILIKE %s) "
                "ORDER BY updated_at DESC",
                (_norm(user_email), q, q),
            ).fetchall()
        return [_row_to_doc(r) for r in rows]

    # ---- entries (scoped to a doc the user owns) --------------------------

    def _owns_doc(self, con, email: str, doc_id: str) -> bool:
        return con.execute(
            f"SELECT 1 FROM {DOCS} WHERE id = %s AND user_email = %s",
            (doc_id, email),
        ).fetchone() is not None

    def add_entry(self, user_email: str, doc_id: str, text: str) -> NoteEntry | None:
        email = _norm(user_email)
        with self._conn() as con:
            if not self._owns_doc(con, email, doc_id):
                return None
            entry = NoteEntry(
                id=_new_id(), doc_id=doc_id, text=text.strip(), created_at=_now()
            )
            con.execute(
                f"INSERT INTO {ENTRIES} (id, doc_id, text, created_at) "
                "VALUES (%s,%s,%s,%s)",
                (entry.id, entry.doc_id, entry.text, entry.created_at),
            )
            con.execute(
                f"UPDATE {DOCS} SET updated_at = %s WHERE id = %s", (_now(), doc_id)
            )
        return entry

    def get_entry(self, user_email: str, entry_id: str) -> NoteEntry | None:
        """Fetch one entry, only if its parent doc belongs to the user."""
        with self._conn() as con:
            row = con.execute(
                f"SELECT e.id, e.doc_id, e.text, e.created_at FROM {ENTRIES} e "
                f"JOIN {DOCS} d ON e.doc_id = d.id "
                "WHERE e.id = %s AND d.user_email = %s",
                (entry_id, _norm(user_email)),
            ).fetchone()
        return _row_to_entry(row) if row else None

    def update_entry(self, user_email: str, entry_id: str, text: str) -> NoteEntry | None:
        email = _norm(user_email)
        with self._conn() as con:
            row = con.execute(
                f"SELECT e.doc_id FROM {ENTRIES} e JOIN {DOCS} d ON e.doc_id = d.id "
                "WHERE e.id = %s AND d.user_email = %s",
                (entry_id, email),
            ).fetchone()
            if not row:
                return None
            doc_id = row[0]
            con.execute(
                f"UPDATE {ENTRIES} SET text = %s WHERE id = %s", (text.strip(), entry_id)
            )
            con.execute(
                f"UPDATE {DOCS} SET updated_at = %s WHERE id = %s", (_now(), doc_id)
            )
            updated = con.execute(
                f"SELECT id, doc_id, text, created_at FROM {ENTRIES} WHERE id = %s",
                (entry_id,),
            ).fetchone()
        return _row_to_entry(updated)

    def delete_entry(self, user_email: str, entry_id: str) -> bool:
        email = _norm(user_email)
        with self._conn() as con:
            row = con.execute(
                f"SELECT e.doc_id FROM {ENTRIES} e JOIN {DOCS} d ON e.doc_id = d.id "
                "WHERE e.id = %s AND d.user_email = %s",
                (entry_id, email),
            ).fetchone()
            if not row:
                return False
            con.execute(f"DELETE FROM {ENTRIES} WHERE id = %s", (entry_id,))
            con.execute(
                f"UPDATE {DOCS} SET updated_at = %s WHERE id = %s", (_now(), row[0])
            )
            return True

    def list_entries(self, user_email: str, doc_id: str) -> list[NoteEntry]:
        email = _norm(user_email)
        with self._conn() as con:
            if not self._owns_doc(con, email, doc_id):
                return []
            rows = con.execute(
                f"SELECT id, doc_id, text, created_at FROM {ENTRIES} "
                "WHERE doc_id = %s ORDER BY created_at ASC",
                (doc_id,),
            ).fetchall()
        return [_row_to_entry(r) for r in rows]


class UserNotesStore:
    """A :class:`NotesStore` bound to one user's email.

    Exposes the same operation names without the leading ``user_email`` argument, so
    the agent (which always acts for a single signed-in user) can call e.g.
    ``store.create_doc(title)`` / ``store.add_entry(doc_id, text)`` directly.
    """

    def __init__(self, store: NotesStore, user_email: str) -> None:
        self._store = store
        self.user_email = _norm(user_email)

    def create_doc(self, title: str, description: str = "") -> NoteDoc:
        return self._store.create_doc(self.user_email, title, description)

    def get_doc(self, doc_id: str) -> NoteDoc | None:
        return self._store.get_doc(self.user_email, doc_id)

    def get_doc_full(self, doc_id: str) -> NoteDoc | None:
        return self._store.get_doc_full(self.user_email, doc_id)

    def update_doc(
        self, doc_id: str, title: str | None = None, description: str | None = None
    ) -> NoteDoc | None:
        return self._store.update_doc(self.user_email, doc_id, title, description)

    def delete_doc(self, doc_id: str) -> bool:
        return self._store.delete_doc(self.user_email, doc_id)

    def list_docs(self) -> list[NoteDoc]:
        return self._store.list_docs(self.user_email)

    def search_docs(self, query: str) -> list[NoteDoc]:
        return self._store.search_docs(self.user_email, query)

    def add_entry(self, doc_id: str, text: str) -> NoteEntry | None:
        return self._store.add_entry(self.user_email, doc_id, text)

    def get_entry(self, entry_id: str) -> NoteEntry | None:
        return self._store.get_entry(self.user_email, entry_id)

    def update_entry(self, entry_id: str, text: str) -> NoteEntry | None:
        return self._store.update_entry(self.user_email, entry_id, text)

    def delete_entry(self, entry_id: str) -> bool:
        return self._store.delete_entry(self.user_email, entry_id)

    def list_entries(self, doc_id: str) -> list[NoteEntry]:
        return self._store.list_entries(self.user_email, doc_id)


def _row_to_doc(row) -> NoteDoc:
    return NoteDoc(
        id=row[0], title=row[1], description=row[2],
        created_at=row[3], updated_at=row[4], entries=None,
    )


def _row_to_entry(row) -> NoteEntry:
    return NoteEntry(id=row[0], doc_id=row[1], text=row[2], created_at=row[3])
