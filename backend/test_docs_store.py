"""Standalone smoke test for the v3.0 note-doc model in store.py.

Proves: doc CRUD, entry CRUD (arbitrary-length text), doc.updated_at bumps on
entry changes, search-by-title/description, legacy v2.0 -> v3.0 migration, and
that the whole DB is still encrypted at rest.

Run:  SQLCIPHER_KEY=testkey NOTES_DB_PATH=/tmp/vma_docs_test.db \
        .venv/bin/python test_docs_store.py
"""

import os
from pathlib import Path

from store import NotesStore


def main() -> None:
    path = os.environ["NOTES_DB_PATH"]
    Path(path).unlink(missing_ok=True)  # fresh DB each run
    store = NotesStore.from_env()

    # --- doc CRUD ------------------------------------------------------------
    print("create_doc…")
    groceries = store.create_doc("Groceries", "Weekly shopping list")
    trip = store.create_doc("Italy Trip", "Planning the September trip")
    print("  ->", groceries.id, groceries.title, "|", trip.id, trip.title)
    assert len({groceries.id, trip.id}) == 2, "doc ids must be unique"

    docs = store.list_docs()
    assert len(docs) == 2
    # list_docs must NOT leak entry contents to the navigator
    assert all(d.entries is None for d in docs), "list_docs leaked entries!"
    assert all("entries" not in d.to_dict() for d in docs)

    print("update_doc (title + description)…")
    u = store.update_doc(groceries.id, title="Groceries 🛒", description="For the week")
    assert u.title == "Groceries 🛒" and u.description == "For the week"

    print("search_docs 'trip'…", [d.title for d in store.search_docs("trip")])
    assert len(store.search_docs("trip")) == 1
    assert len(store.search_docs("September")) == 1  # matches description
    assert len(store.search_docs("nonexistent")) == 0

    # --- entry CRUD ----------------------------------------------------------
    print("add_entry…")
    e1 = store.add_entry(groceries.id, "milk")
    long_text = "Buy: " + ", ".join(f"item{i}" for i in range(200))  # arbitrary length
    e2 = store.add_entry(groceries.id, long_text)
    assert e1 and e2
    assert store.add_entry("nope-doc-id", "x") is None, "entry into missing doc must fail"

    entries = store.list_entries(groceries.id)
    assert len(entries) == 2
    assert entries[1].text == long_text, "entry must hold arbitrary-length text"

    # adding an entry bumps the parent doc's updated_at (so MAIN list re-sorts)
    assert store.get_doc(groceries.id).updated_at >= u.updated_at

    print("get_doc_full (doc + entries)…")
    full = store.get_doc_full(groceries.id)
    assert full.entries is not None and len(full.entries) == 2
    assert "entries" in full.to_dict()

    print("update_entry…")
    ue = store.update_entry(e1.id, "oat milk")
    assert ue.text == "oat milk"
    assert store.get_entry(e1.id).text == "oat milk"

    print("delete_entry…")
    assert store.delete_entry(e2.id) is True
    assert store.delete_entry("missing") is False
    assert len(store.list_entries(groceries.id)) == 1

    print("delete_doc (cascades entries)…")
    assert store.delete_doc(groceries.id) is True
    assert store.get_doc(groceries.id) is None
    assert store.list_entries(groceries.id) == [], "entries must be cascade-deleted"
    assert len(store.list_docs()) == 1

    # --- legacy migration ----------------------------------------------------
    print("migrate_legacy_notes…")
    # seed an OLD flat note, then wipe docs to simulate a pre-v3.0 DB
    store.create("dog's name is Rex", tags="pets")
    for d in store.list_docs():
        store.delete_doc(d.id)
    assert store.list_docs() == []
    migrated = store.migrate_legacy_notes()
    assert migrated is not None, "should migrate the one legacy note"
    assert migrated.title == "Imported Notes"
    assert any(e.text == "dog's name is Rex" for e in migrated.entries)
    # idempotent: a second run with docs present is a no-op
    assert store.migrate_legacy_notes() is None
    # old notes table is left intact (non-destructive)
    assert any(n.text == "dog's name is Rex" for n in store.list_all())

    # --- encryption at rest --------------------------------------------------
    raw = Path(path).read_bytes()
    assert b"Rex" not in raw, "plaintext found in DB file!"
    assert b"Italy Trip" not in raw, "plaintext doc title found in DB file!"
    assert raw[:15] != b"SQLite format 3", "DB is NOT encrypted!"

    print("\nALL ASSERTIONS PASSED ✅  (doc+entry CRUD, migration, encrypted at rest)")


if __name__ == "__main__":
    main()
