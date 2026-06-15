"""Standalone smoke test for store.py — proves the encrypted CRUD works.

Run:  SQLCIPHER_KEY=testkey NOTES_DB_PATH=/tmp/vma_test.db python test_store.py
"""

import os
from pathlib import Path

from store import NotesStore


def main() -> None:
    path = os.environ["NOTES_DB_PATH"]
    Path(path).unlink(missing_ok=True)  # fresh DB each run
    store = NotesStore.from_env()

    print("create…")
    n1 = store.create("dog's name is Rex", tags="pets")
    n2 = store.create("I live in Seattle", tags="location")
    print("  ->", n1.id, n1.text, "|", n2.id, n2.text)

    print("list_all…", [(n.id, n.text) for n in store.list_all()])
    assert len(store.list_all()) == 2

    print("get…", store.get(n1.id))
    assert store.get(n1.id).text == "dog's name is Rex"

    print("update…")
    u = store.update(n1.id, text="dog's name is Rex, a husky")
    print("  ->", u.text)
    assert "husky" in store.get(n1.id).text
    assert u.updated_at >= u.created_at

    print("search 'seattle'…", [(n.id, n.text) for n in store.search("seattle")])
    assert len(store.search("seattle")) == 1
    assert len(store.search("xyz")) == 0

    print("delete…", store.delete(n2.id))
    assert store.get(n2.id) is None
    assert len(store.list_all()) == 1

    print("delete_all…", store.delete_all(), "rows")
    assert store.list_all() == []

    # prove encryption at rest
    raw = Path(path).read_bytes()
    assert b"Rex" not in raw, "plaintext found in DB file!"
    assert raw[:15] != b"SQLite format 3", "DB is NOT encrypted!"
    print("\nALL ASSERTIONS PASSED ✅  (encrypted at rest, no plaintext in file)")


if __name__ == "__main__":
    main()
