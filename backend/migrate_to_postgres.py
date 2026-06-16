"""One-time data migration: local encrypted SQLite -> Supabase PostgreSQL.

Reads the local SQLCipher-encrypted ``notes.db`` (via :class:`SqliteNotesStore`,
so ``SQLCIPHER_KEY`` / ``NOTES_DB_PATH`` must be set) and writes every note doc,
entry, and legacy flat note into the Postgres store (:class:`PgNotesStore`,
driven by ``DB_URL`` / ``DB_SCHEMA`` / ``DB_SSL_REQUIRE``), PRESERVING ids and
timestamps so the data-channel JSON contract is unchanged.

Idempotent: every row is an ``INSERT ... ON CONFLICT (id) DO NOTHING`` upsert, so
re-running never duplicates or clobbers anything. The source SQLite DB is only
read, never modified.

Run (from backend/, with .env populated):
    .venv/bin/python migrate_to_postgres.py
"""

from __future__ import annotations

from dotenv import load_dotenv

from store import PgNotesStore, SqliteNotesStore

load_dotenv()


def main() -> None:
    print("Opening source (SQLite/SQLCipher) …")
    src = SqliteNotesStore.from_env()

    print("Opening destination (PostgreSQL) …")
    dst = PgNotesStore.from_env()
    print(f"  -> schema: {dst.schema}")

    docs = src.list_docs()
    legacy = src.list_all()
    print(f"Source contains {len(docs)} doc(s) and {len(legacy)} legacy flat note(s).")

    n_docs = n_entries = n_notes = 0

    for meta in docs:
        full = src.get_doc_full(meta.id)
        if full is None:
            continue
        dst.import_doc(full)
        n_docs += 1
        for entry in (full.entries or []):
            dst.import_entry(entry)
            n_entries += 1

    for note in legacy:
        dst.import_note(note)
        n_notes += 1

    print("\nMigration summary (upserted, idempotent):")
    print(f"  docs:          {n_docs}")
    print(f"  entries:       {n_entries}")
    print(f"  legacy notes:  {n_notes}")

    print("\nDestination now contains:")
    for d in dst.list_docs():
        cnt = len(dst.list_entries(d.id))
        print(f"  [{d.id}] {d.title!r} — {d.description!r} "
              f"({cnt} entr{'y' if cnt == 1 else 'ies'})")

    dst.close()
    print("\nDone ✅")


if __name__ == "__main__":
    main()
