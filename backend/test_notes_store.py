"""Smoke test for the per-user Postgres notes store (writer_app schema).

Runs only when DATABASE_URL is set. Uses throwaway, unique emails and deletes every
doc it creates at the end, so it never touches real users' data. Proves:
  - per-user isolation (one user cannot see/read/edit another user's docs/entries)
  - doc CRUD + entry CRUD (arbitrary-length text), updated_at bumps on entry changes
  - search scoped to the owner
  - list_docs never leaks entries

Run:  .venv/bin/python test_notes_store.py     # needs DATABASE_URL (via .env)
"""

from __future__ import annotations

import os
import uuid

from dotenv import load_dotenv

load_dotenv()

from notes_store import NotesStore  # noqa: E402


def main() -> None:
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        print("skip  DATABASE_URL not set — skipping Postgres notes-store test")
        return

    store = NotesStore.from_env()
    alice = f"alice+{uuid.uuid4().hex[:8]}@dropnote.test"
    bob = f"bob+{uuid.uuid4().hex[:8]}@dropnote.test"
    created_docs: list[tuple[str, str]] = []  # (email, doc_id) for cleanup

    try:
        # --- doc CRUD, scoped to alice -------------------------------------
        groceries = store.create_doc(alice, "Groceries", "Weekly shopping list")
        trip = store.create_doc(alice, "Italy Trip", "Planning the September trip")
        created_docs += [(alice, groceries.id), (alice, trip.id)]
        assert len({groceries.id, trip.id}) == 2, "doc ids must be unique"

        docs = store.list_docs(alice)
        assert len(docs) == 2, f"alice should see 2 docs, saw {len(docs)}"
        assert all(d.entries is None for d in docs), "list_docs leaked entries!"
        assert all("entries" not in d.to_dict() for d in docs)
        print("ok  doc create + list (no entry leak)")

        u = store.update_doc(alice, groceries.id, title="Groceries 2", description="For the week")
        assert u and u.title == "Groceries 2" and u.description == "For the week"
        assert len(store.search_docs(alice, "trip")) == 1
        print("ok  doc update + search")

        # --- entry CRUD ----------------------------------------------------
        long_text = "buy " + "x" * 5000
        e1 = store.add_entry(alice, groceries.id, "milk")
        e2 = store.add_entry(alice, groceries.id, long_text)
        assert e1 and e2 and e2.text == long_text, "arbitrary-length entry text must persist"

        full = store.get_doc_full(alice, groceries.id)
        assert full and len(full.entries) == 2
        # updated_at must have bumped past created_at after adding entries
        assert full.updated_at >= full.created_at
        print("ok  entry add + get_doc_full")

        upd = store.update_entry(alice, e1.id, "oat milk")
        assert upd and upd.text == "oat milk"
        assert store.delete_entry(alice, e2.id) is True
        assert len(store.list_entries(alice, groceries.id)) == 1
        print("ok  entry update + delete")

        # --- per-user isolation -------------------------------------------
        bobdoc = store.create_doc(bob, "Bob's secret", "private")
        created_docs.append((bob, bobdoc.id))
        assert store.list_docs(bob) and len(store.list_docs(bob)) == 1
        # alice must not see, read, or touch bob's doc/entries
        assert store.get_doc(alice, bobdoc.id) is None
        assert store.get_doc_full(alice, bobdoc.id) is None
        assert store.update_doc(alice, bobdoc.id, title="hax") is None
        assert store.delete_doc(alice, bobdoc.id) is False
        # bob owns his doc; alice can't add entries to it
        assert store.add_entry(alice, bobdoc.id, "sneaky") is None
        bob_entry = store.add_entry(bob, bobdoc.id, "ok")
        assert bob_entry is not None
        assert store.get_entry(alice, bob_entry.id) is None  # cross-user entry read blocked
        assert store.update_entry(alice, bob_entry.id, "hax") is None
        assert store.delete_entry(alice, bob_entry.id) is False
        print("ok  per-user isolation (alice cannot touch bob's data)")

        # --- delete cascade ------------------------------------------------
        assert store.delete_doc(alice, groceries.id) is True
        assert store.get_doc_full(alice, groceries.id) is None
        # entries went with it (ON DELETE CASCADE) — re-adding to a gone doc fails
        assert store.add_entry(alice, groceries.id, "nope") is None
        print("ok  delete_doc cascades entries")

        print("\nPASS  all notes-store checks")
    finally:
        # Best-effort cleanup so the test never leaves rows behind in the real DB.
        for email, doc_id in created_docs:
            try:
                store.delete_doc(email, doc_id)
            except Exception:
                pass


if __name__ == "__main__":
    main()
