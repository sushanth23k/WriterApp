"""Voice Memory Assistant — LiveKit Agent worker.

Pipeline: Deepgram STT (nova-3) -> Groq LLM (llama-3.3-70b-versatile) -> Deepgram
Aura TTS, with Silero VAD for turn detection / barge-in.

v2.0: memory now PERSISTS across conversations, backed by an encrypted SQLCipher
store (store.py). The existing v1.0 tools (remember_fact / recall_facts /
forget_everything) are preserved and re-backed on the store; new tools add full
CRUD. Every mutation is broadcast to the room over the "notes" data topic so the
app's notes view updates live, and inbound edits the app sends on the "notes-edit"
topic are applied to the store and re-broadcast.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import deepgram, groq, silero

from store import Note, NotesStore

load_dotenv()

logger = logging.getLogger("voice-memory-assistant")

NOTES_TOPIC = "notes"          # agent -> app: full notes list after any change
NOTES_EDIT_TOPIC = "notes-edit"  # app -> agent: user typed an edit


@dataclass
class SessionMemory:
    """Per-session handles. The notes themselves live in the persistent store."""

    store: NotesStore
    room: rtc.Room | None = None


INSTRUCTIONS = (
    "You are Voice Memory Assistant, a calm, concise, friendly voice companion. "
    "You speak out loud, so keep replies short and natural — one or two sentences, "
    "no markdown, no bullet points, no emoji. "
    "You remember notes the user tells you, and your memory PERSISTS across "
    "conversations. "
    "When the user tells you something to remember (e.g. 'remember that ...'), call "
    "remember_fact. When they ask what you know (e.g. 'what do you know so far?'), call "
    "recall_facts and read the notes back conversationally. When they ask you to forget "
    "everything, call forget_everything and confirm briefly. "
    "To change an existing note call update_note, and to remove a single note call "
    "delete_note. Use list_notes when you need each note's id before updating or deleting "
    "— ids are internal bookkeeping, so NEVER read an id out loud. "
    "If asked about persistence, explain that you keep these notes saved and will "
    "remember them next time too."
)


def _summarize_for_context(notes: list[Note]) -> str:
    if not notes:
        return "You currently have no saved notes from past conversations."
    lines = [f"- [{n.id}] {n.text}" + (f" (tags: {n.tags})" if n.tags else "") for n in notes]
    return (
        "Here is what you already remember from past conversations "
        "(persisted notes). Use these when the user asks what you know:\n"
        + "\n".join(lines)
    )


class MemoryAssistant(Agent):
    def __init__(self, existing_notes: list[Note]) -> None:
        instructions = INSTRUCTIONS + "\n\n" + _summarize_for_context(existing_notes)
        super().__init__(instructions=instructions)

    # ---- v1.0 tools, preserved by name, re-backed on the persistent store ----

    @function_tool
    async def remember_fact(self, context: RunContext[SessionMemory], fact: str) -> str:
        """Store a single note the user wants remembered (persists across conversations).

        Args:
            fact: The note to remember, phrased concisely (e.g. "the user's dog is named Rex").
        """
        fact = fact.strip()
        if not fact:
            return "There was nothing to remember."
        note = context.userdata.store.create(fact)
        logger.info("remember_fact -> created note %s: %r", note.id, note.text)
        await broadcast_notes(context.userdata)
        return f"Got it. I've saved that: {fact}."

    @function_tool
    async def recall_facts(self, context: RunContext[SessionMemory]) -> str:
        """Return everything currently remembered (across all conversations)."""
        notes = context.userdata.store.list_all()
        logger.info("recall_facts: returning %d note(s)", len(notes))
        if not notes:
            return "I don't have anything saved yet."
        listed = "; ".join(n.text for n in notes)
        return f"Here's what I'm remembering: {listed}."

    @function_tool
    async def forget_everything(self, context: RunContext[SessionMemory]) -> str:
        """Erase ALL saved notes (permanently, across conversations)."""
        count = context.userdata.store.delete_all()
        logger.info("forget_everything: cleared %d note(s)", count)
        await broadcast_notes(context.userdata)
        return "Done. I've forgotten everything I had saved."

    # ---- v2.0 new tools ----

    @function_tool
    async def list_notes(self, context: RunContext[SessionMemory]) -> str:
        """List every saved note WITH its id, so you can update or delete a specific one.

        Returns ids for your internal use only — do not read ids aloud to the user.
        """
        notes = context.userdata.store.list_all()
        logger.info("list_notes: %d note(s)", len(notes))
        if not notes:
            return "No notes saved."
        return "\n".join(f"id={n.id} | {n.text}" for n in notes)

    @function_tool
    async def update_note(
        self, context: RunContext[SessionMemory], note_id: str, text: str
    ) -> str:
        """Change the text of an existing note.

        Args:
            note_id: The id of the note to change (get it from list_notes).
            text: The new text for the note.
        """
        updated = context.userdata.store.update(note_id.strip(), text=text.strip())
        if not updated:
            logger.info("update_note: no note with id %r", note_id)
            return f"I couldn't find a note with id {note_id}."
        logger.info("update_note %s -> %r", updated.id, updated.text)
        await broadcast_notes(context.userdata)
        return f"Updated. It now says: {updated.text}."

    @function_tool
    async def delete_note(self, context: RunContext[SessionMemory], note_id: str) -> str:
        """Delete a single note by id.

        Args:
            note_id: The id of the note to delete (get it from list_notes).
        """
        ok = context.userdata.store.delete(note_id.strip())
        logger.info("delete_note %s -> %s", note_id, ok)
        if not ok:
            return f"I couldn't find a note with id {note_id}."
        await broadcast_notes(context.userdata)
        return "Deleted that note."


# ---- Live sync helpers --------------------------------------------------------


async def broadcast_notes(ud: SessionMemory) -> None:
    """Publish the full notes list to the room on the 'notes' topic (reliable)."""
    if ud.room is None:
        return
    payload = json.dumps(
        {"type": "notes", "notes": [n.to_dict() for n in ud.store.list_all()]}
    )
    try:
        await ud.room.local_participant.publish_data(
            payload, reliable=True, topic=NOTES_TOPIC
        )
        logger.info("broadcast_notes: published %d byte(s)", len(payload))
    except Exception:
        logger.exception("broadcast_notes failed")


async def _apply_inbound_edit(ud: SessionMemory, data: bytes) -> None:
    """Apply an edit the app typed (topic 'notes-edit'), then re-broadcast."""
    try:
        msg = json.loads(data.decode("utf-8"))
    except Exception:
        logger.exception("notes-edit: bad JSON")
        return
    action = msg.get("action")
    logger.info("notes-edit inbound: %s %s", action, {k: v for k, v in msg.items() if k != "action"})
    try:
        if action == "create":
            ud.store.create(msg.get("text", ""), msg.get("tags", ""))
        elif action == "update":
            ud.store.update(msg["id"], text=msg.get("text"), tags=msg.get("tags"))
        elif action == "delete":
            ud.store.delete(msg["id"])
        elif action == "delete_all":
            ud.store.delete_all()
        else:
            logger.warning("notes-edit: unknown action %r", action)
            return
    except Exception:
        logger.exception("notes-edit: failed to apply %r", action)
        return
    await broadcast_notes(ud)


async def entrypoint(ctx: JobContext) -> None:
    store = NotesStore.from_env()
    userdata = SessionMemory(store=store, room=ctx.room)

    existing = store.list_all()
    logger.info("session start: %d existing note(s) loaded from store", len(existing))

    session: AgentSession[SessionMemory] = AgentSession(
        userdata=userdata,
        vad=silero.VAD.load(),
        stt=deepgram.STT(model="nova-3", language="en-US"),
        llm=groq.LLM(model="llama-3.3-70b-versatile"),
        tts=deepgram.TTS(model="aura-2-thalia-en"),
    )

    # Inbound edits typed in the app: apply to store + re-broadcast.
    def _on_data(packet: rtc.DataPacket) -> None:
        if packet.topic == NOTES_EDIT_TOPIC:
            asyncio.create_task(_apply_inbound_edit(userdata, packet.data))

    ctx.room.on("data_received", _on_data)

    await session.start(agent=MemoryAssistant(existing_notes=existing), room=ctx.room)

    # Push current notes to the app immediately so its panel is populated on connect.
    await broadcast_notes(userdata)

    greet = (
        "Greet the user warmly in one short sentence and invite them to tell you "
        "something to remember."
    )
    if existing:
        greet = (
            "Greet the user warmly in one short sentence, mention you still remember "
            f"the {len(existing)} thing(s) they saved before, and invite them to continue."
        )
    await session.generate_reply(instructions=greet)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
