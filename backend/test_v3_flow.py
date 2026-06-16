"""Headless end-to-end test of the v3.0 data flows against the running stack.

No mic/app needed. Exercises, over the LiveKit data channel:
  MAIN : receive 'docs' list (no entries leaked), typed doc create/update/delete.
  NOTE : receive the single 'doc' (with entries), typed add/update/delete entry +
         update_meta, and CONTEXT ISOLATION (a note room only ever sees its own doc,
         and refuses edits aimed at another doc's entries).

Run (stack up):  .venv/bin/python test_v3_flow.py
"""

import asyncio
import json
import time
import urllib.request

from livekit import rtc

TOKEN_SERVER = "http://localhost:8080/token"


def tok(body: dict) -> dict:
    req = urllib.request.Request(
        TOKEN_SERVER, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class Client:
    """Joins a room, collects data messages by topic, can publish edits."""

    def __init__(self) -> None:
        self.room = rtc.Room()
        self.msgs: dict[str, list] = {}

        @self.room.on("data_received")
        def _on(packet: rtc.DataPacket) -> None:
            try:
                self.msgs.setdefault(packet.topic, []).append(
                    json.loads(packet.data.decode())
                )
            except Exception:
                pass

    async def join(self, body: dict) -> dict:
        d = tok(body)
        await self.room.connect(d["url"], d["token"])
        return d

    async def wait(self, topic: str, pred, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.msgs.get(topic, []):
                if pred(m):
                    return m
            await asyncio.sleep(0.2)
        return None

    def last(self, topic: str):
        return self.msgs.get(topic, [None])[-1]

    async def pub(self, topic: str, obj: dict):
        await self.room.local_participant.publish_data(
            json.dumps(obj).encode(), reliable=True, topic=topic
        )

    async def close(self):
        await self.room.disconnect()


def check(label: str, cond: bool):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        raise SystemExit(f"FAILED: {label}")


async def test_main() -> str:
    print("\n=== MAIN state ===")
    c = Client()
    await c.join({"mode": "main"})
    first = await c.wait("docs", lambda m: m.get("type") == "docs")
    check("received initial docs list", first is not None)
    docs0 = first["docs"]
    check("docs carry id/title/description", all(
        {"id", "title", "description"} <= set(d) for d in docs0))
    check("docs list does NOT leak entries", all("entries" not in d for d in docs0))
    n0 = len(docs0)

    marker = f"Trip {int(time.time())}"
    await c.pub("docs-edit", {"action": "create", "title": marker,
                              "description": "headless-created"})
    after = await c.wait("docs", lambda m: any(d["title"] == marker for d in m["docs"]))
    check("typed doc create re-broadcast", after is not None)
    new = next(d for d in after["docs"] if d["title"] == marker)
    check("doc count grew by 1", len(after["docs"]) == n0 + 1)
    new_id = new["id"]

    await c.pub("docs-edit", {"action": "update", "id": new_id,
                              "title": marker + " (renamed)"})
    renamed = await c.wait("docs", lambda m: any(
        d["id"] == new_id and d["title"].endswith("(renamed)") for d in m["docs"]))
    check("typed doc rename re-broadcast", renamed is not None)

    await c.pub("docs-edit", {"action": "delete", "id": new_id})
    deleted = await c.wait("docs", lambda m: all(d["id"] != new_id for d in m["docs"]))
    check("typed doc delete re-broadcast", deleted is not None)
    check("doc count back to baseline", len(deleted["docs"]) == n0)

    await c.close()
    # return an existing doc id to use for the note tests (create two fresh ones)
    return None


async def make_doc(title: str, desc: str) -> str:
    """Create a doc via a short-lived main session and return its id."""
    c = Client()
    await c.join({"mode": "main"})
    await c.wait("docs", lambda m: m.get("type") == "docs")
    await c.pub("docs-edit", {"action": "create", "title": title, "description": desc})
    got = await c.wait("docs", lambda m: any(d["title"] == title for d in m["docs"]))
    doc_id = next(d["id"] for d in got["docs"] if d["title"] == title)
    await c.close()
    return doc_id


async def test_note_and_isolation():
    print("\n=== NOTE state + isolation ===")
    ts = int(time.time())
    doc_a = await make_doc(f"DocA-{ts}", "note under test")
    doc_b = await make_doc(f"DocB-{ts}", "the OTHER note (must stay isolated)")

    # seed an entry in B so we have a foreign entry id to attack with
    cb = Client()
    await cb.join({"mode": "note", "doc_id": doc_b})
    await cb.wait("doc", lambda m: m.get("type") == "doc")
    await cb.pub("doc-edit", {"action": "add_entry", "text": "B-secret-entry"})
    b_doc = await cb.wait("doc", lambda m: any(
        e["text"] == "B-secret-entry" for e in (m["doc"]["entries"] or [])))
    b_entry_id = next(e["id"] for e in b_doc["doc"]["entries"]
                      if e["text"] == "B-secret-entry")
    await cb.close()

    # open A and verify isolation: payload is ONLY doc A
    ca = Client()
    await ca.join({"mode": "note", "doc_id": doc_a})
    a0 = await ca.wait("doc", lambda m: m.get("type") == "doc")
    check("NOTE receives a single doc (not a list)", isinstance(a0["doc"], dict))
    check("NOTE doc is exactly the requested doc", a0["doc"]["id"] == doc_a)
    check("NOTE doc carries its entries", "entries" in a0["doc"])

    await ca.pub("doc-edit", {"action": "add_entry", "text": "milk"})
    a1 = await ca.wait("doc", lambda m: any(
        e["text"] == "milk" for e in (m["doc"]["entries"] or [])))
    check("typed add_entry re-broadcast", a1 is not None)
    a_entry_id = next(e["id"] for e in a1["doc"]["entries"] if e["text"] == "milk")

    await ca.pub("doc-edit", {"action": "update_entry", "id": a_entry_id, "text": "oat milk"})
    a2 = await ca.wait("doc", lambda m: any(
        e["text"] == "oat milk" for e in (m["doc"]["entries"] or [])))
    check("typed update_entry re-broadcast", a2 is not None)

    await ca.pub("doc-edit", {"action": "update_meta", "description": "groceries now"})
    a3 = await ca.wait("doc", lambda m: m["doc"]["description"] == "groceries now")
    check("typed update_meta re-broadcast", a3 is not None)

    # ISOLATION ATTACK: from A's room, try to edit B's entry — must be ignored.
    await ca.pub("doc-edit", {"action": "update_entry", "id": b_entry_id,
                              "text": "HACKED"})
    await asyncio.sleep(2)
    # A's broadcast must never contain B's entry id
    check("A never sees B's entry", all(
        e["id"] != b_entry_id for e in (ca.last("doc")["doc"]["entries"] or [])))
    await ca.close()

    # Re-open B and confirm its entry is UNCHANGED (the cross-doc edit was rejected)
    cb2 = Client()
    await cb2.join({"mode": "note", "doc_id": doc_b})
    bcheck = await cb2.wait("doc", lambda m: m.get("type") == "doc")
    b_entry = next((e for e in bcheck["doc"]["entries"] if e["id"] == b_entry_id), None)
    check("B's entry untouched by A's room", b_entry is not None
          and b_entry["text"] == "B-secret-entry")
    await cb2.close()

    # cleanup the two test docs
    cc = Client()
    await cc.join({"mode": "main"})
    await cc.wait("docs", lambda m: m.get("type") == "docs")
    for d in (doc_a, doc_b):
        await cc.pub("docs-edit", {"action": "delete", "id": d})
    await asyncio.sleep(1.5)
    await cc.close()


async def main():
    await test_main()
    await test_note_and_isolation()
    print("\nALL v3.0 FLOW ASSERTIONS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
