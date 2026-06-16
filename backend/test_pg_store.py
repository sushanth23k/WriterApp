"""Smoke test for the PostgreSQL backend (PgNotesStore).

Runs against a THROWAWAY schema (default ``writer_app_test``) so it never touches
real data, then drops that schema on the way out. Exercises the same doc/entry
CRUD + context-isolation guards as the SQLite tests, proving the PG backend
matches behavior.

Requires a reachable Postgres (Supabase) via the env in .env:
    DB_URL (session pooler :5432)   [or DB_POOL_URL]
    DB_SSL_REQUIRE=true

Run (from backend/):
    DB_SCHEMA=writer_app_test .venv/bin/python test_pg_store.py
(If DB_SCHEMA is unset this test forces ``writer_app_test`` itself.)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# Force a throwaway schema BEFORE importing/instantiating the store, unless the
# caller already picked a non-default test schema.
_schema = os.environ.get("DB_SCHEMA", "")
if not _schema or _schema == "writer_app":
    os.environ["DB_SCHEMA"] = "writer_app_test"

from store import PgNotesStore  # noqa: E402


def check(label: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        raise SystemExit(f"FAILED: {label}")


def _drop_test_schema(store: PgNotesStore) -> None:
    with store.pool.connection() as con:
        con.execute(f"DROP SCHEMA IF EXISTS {store.schema} CASCADE")


def main() -> None:
    store = PgNotesStore.from_env()
    assert store.schema != "writer_app", "refusing to run against the real schema"
    print(f"Using throwaway schema: {store.schema}")

    # Start clean even if a prior aborted run left rows behind.
    _drop_test_schema(store)
    store._init_schema()

    try:
        # --- doc CRUD --------------------------------------------------------
        print("create_doc…")
        groceries = store.create_doc("Groceries", "Weekly shopping list")
        trip = store.create_doc("Italy Trip", "Planning the September trip")
        check("doc ids unique", len({groceries.id, trip.id}) == 2)

        docs = store.list_docs()
        check("list_docs returns 2", len(docs) == 2)
        check("list_docs does NOT leak entries", all(d.entries is None for d in docs))
        check("list_docs dict has no 'entries' key",
              all("entries" not in d.to_dict() for d in docs))

        print("update_doc…")
        u = store.update_doc(groceries.id, title="Groceries 🛒", description="For the week")
        check("update_doc applied", u.title == "Groceries 🛒" and u.description == "For the week")

        print("search_docs…")
        check("search by title", len(store.search_docs("trip")) == 1)
        check("search by description", len(store.search_docs("September")) == 1)
        check("search miss", len(store.search_docs("nonexistent")) == 0)

        # --- entry CRUD ------------------------------------------------------
        print("add_entry…")
        e1 = store.add_entry(groceries.id, "milk")
        long_text = "Buy: " + ", ".join(f"item{i}" for i in range(200))
        e2 = store.add_entry(groceries.id, long_text)
        check("entries created", bool(e1 and e2))
        check("entry into missing doc fails", store.add_entry("nope-doc", "x") is None)

        entries = store.list_entries(groceries.id)
        check("two entries listed", len(entries) == 2)
        check("arbitrary-length text preserved", entries[1].text == long_text)
        check("add_entry bumps doc.updated_at",
              store.get_doc(groceries.id).updated_at >= u.updated_at)

        print("get_doc_full…")
        full = store.get_doc_full(groceries.id)
        check("get_doc_full carries entries", full.entries is not None and len(full.entries) == 2)
        check("get_doc_full dict has 'entries'", "entries" in full.to_dict())

        print("update_entry…")
        ue = store.update_entry(e1.id, "oat milk")
        check("update_entry applied", ue.text == "oat milk" and store.get_entry(e1.id).text == "oat milk")

        print("delete_entry…")
        check("delete real entry", store.delete_entry(e2.id) is True)
        check("delete missing entry", store.delete_entry("missing") is False)
        check("one entry remains", len(store.list_entries(groceries.id)) == 1)

        print("delete_doc (FK cascade)…")
        check("delete_doc", store.delete_doc(groceries.id) is True)
        check("doc gone", store.get_doc(groceries.id) is None)
        check("entries cascade-deleted", store.list_entries(groceries.id) == [])
        check("one doc remains", len(store.list_docs()) == 1)

        # --- isolation: an entry id always belongs to exactly one doc --------
        print("isolation guard…")
        a = store.create_doc("DocA", "under test")
        b = store.create_doc("DocB", "the other")
        ea = store.add_entry(a.id, "A-entry")
        eb = store.add_entry(b.id, "B-secret")
        check("A only lists its own entry",
              [e.text for e in store.list_entries(a.id)] == ["A-entry"])
        check("B only lists its own entry",
              [e.text for e in store.list_entries(b.id)] == ["B-secret"])
        check("entry resolves to its true owner", store.get_entry(eb.id).doc_id == b.id)
        store.delete_doc(a.id)
        check("deleting A leaves B's entry intact", store.get_entry(eb.id) is not None)

        # --- legacy flat notes + migrate_legacy_notes ------------------------
        print("legacy notes + migration…")
        store.delete_doc(b.id)
        for d in store.list_docs():
            store.delete_doc(d.id)
        check("no docs left before migration", store.list_docs() == [])
        store.create("dog's name is Rex", tags="pets")
        migrated = store.migrate_legacy_notes()
        check("legacy note migrated", migrated is not None and migrated.title == "Imported Notes")
        check("migrated entry present",
              any(e.text == "dog's name is Rex" for e in (migrated.entries or [])))
        check("migration idempotent", store.migrate_legacy_notes() is None)
        check("legacy table untouched",
              any(n.text == "dog's name is Rex" for n in store.list_all()))

        print("\nALL PG ASSERTIONS PASSED ✅")
    finally:
        _drop_test_schema(store)
        store.close()
        print("(throwaway schema dropped)")


if __name__ == "__main__":
    main()
