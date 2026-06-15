"""Headless verification of the inbound typed-edit path (no mic, no app needed).

Simulates what the iOS app does when the user TYPES a note:
  1. fetch a token, join the LiveKit room (this triggers agent dispatch)
  2. listen on the "notes" topic for the agent's broadcasts
  3. publish a "notes-edit" create on the "notes-edit" topic
  4. confirm the agent applied it to the store and re-broadcast the new note

Run:  python test_notes_edit.py
"""

import asyncio
import json
import time
import urllib.request

from livekit import rtc

TOKEN_SERVER = "http://localhost:8080/token"


def get_token() -> tuple[str, str, str]:
    req = urllib.request.Request(
        TOKEN_SERVER, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    return d["url"], d["token"], d["room"]


async def main() -> None:
    url, token, room_name = get_token()
    print(f"joining room {room_name} at {url}")

    room = rtc.Room()
    broadcasts: list[list[dict]] = []

    @room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:
        if packet.topic == "notes":
            msg = json.loads(packet.data.decode())
            broadcasts.append(msg.get("notes", []))
            print(f"  ← notes broadcast: {len(msg.get('notes', []))} note(s)")

    await room.connect(url, token)
    print("connected; waiting for agent to join + initial broadcast…")

    # wait for the agent to join and send the initial notes broadcast
    deadline = time.time() + 20
    while not broadcasts and time.time() < deadline:
        await asyncio.sleep(0.3)
    if not broadcasts:
        print("FAIL: no initial broadcast from agent (is the worker running?)")
        await room.disconnect()
        return
    before = len(broadcasts[-1])

    # publish a typed edit (create), exactly like the app's "Add" button
    marker = f"typed-edit-test-{int(time.time())}"
    payload = json.dumps({"action": "create", "text": marker}).encode()
    print(f"  → publishing notes-edit create: {marker!r}")
    await room.local_participant.publish_data(payload, reliable=True, topic="notes-edit")

    # wait for the re-broadcast that includes our new note
    deadline = time.time() + 15
    found = False
    while time.time() < deadline:
        if broadcasts and any(n.get("text") == marker for n in broadcasts[-1]):
            found = True
            break
        await asyncio.sleep(0.3)

    after = len(broadcasts[-1])
    print(f"\nbefore={before} after={after} contains_marker={found}")
    if found and after == before + 1:
        print("PASS ✅  typed edit reached the store and was re-broadcast to all clients")
    else:
        print("FAIL ❌  typed edit did not round-trip as expected")

    await room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
