"""One-shot migration runner: v2.0 flat notes -> v3.0 note docs + entries.

Opens the encrypted store from the environment (SQLCIPHER_KEY / NOTES_DB_PATH via
.env), ensures the v3.0 schema exists, and folds any legacy flat notes into a
single "Imported Notes" doc. Safe to run repeatedly — it is idempotent and never
drops the old ``notes`` table.

Run:  .venv/bin/python migrate.py
"""

from __future__ import annotations

from dotenv import load_dotenv

from store import NotesStore

load_dotenv()


def main() -> None:
    store = NotesStore.from_env()  # _init_schema() adds the v3.0 tables if missing

    legacy = store.list_all()
    docs_before = store.list_docs()
    print(f"legacy flat notes: {len(legacy)}")
    print(f"existing docs:     {len(docs_before)}")

    migrated = store.migrate_legacy_notes()
    if migrated is None:
        if docs_before:
            print("nothing to migrate (docs already present) — no-op.")
        else:
            print("nothing to migrate (no legacy notes).")
    else:
        print(
            f"migrated {len(migrated.entries)} legacy note(s) into doc "
            f"'{migrated.title}' (id={migrated.id})."
        )

    print("\ndocs now:")
    for d in store.list_docs():
        n = len(store.list_entries(d.id))
        print(f"  [{d.id}] {d.title!r} — {d.description!r} ({n} entr{'y' if n == 1 else 'ies'})")


if __name__ == "__main__":
    main()
