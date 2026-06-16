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

# --- v2.0 legacy topics (unchanged) ---
NOTES_TOPIC = "notes"          # agent -> app: full notes list after any change
NOTES_EDIT_TOPIC = "notes-edit"  # app -> agent: user typed an edit

# --- v3.0 MAIN-state topics (navigator over the docs LIST) ---
DOCS_TOPIC = "docs"            # agent -> app: full docs list (id/title/description)
DOCS_EDIT_TOPIC = "docs-edit"  # app -> agent: typed doc CRUD (create/update/delete)
NAVIGATE_TOPIC = "navigate"    # agent -> app: transition into NOTE state for a doc id

# --- v3.0 NOTE-state topics (one isolated doc) ---
DOC_TOPIC = "doc"              # agent -> app: the single current doc (with entries)
DOC_EDIT_TOPIC = "doc-edit"    # app -> agent: typed entry/meta CRUD for THIS doc


@dataclass
class SessionMemory:
    """Per-session handles. The notes themselves live in the persistent store.

    ``mode`` selects which agent is running: "legacy" (v2.0 MemoryAssistant),
    "main" (NavigatorAgent over the docs list) or "note" (NoteAgent on one doc).
    ``doc_id`` is only set in "note" mode and scopes every operation to that doc.
    """

    store: NotesStore
    room: rtc.Room | None = None
    mode: str = "legacy"
    doc_id: str | None = None


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


# =====================================================================
# v3.0 MAIN state — NavigatorAgent (context = docs LIST only)
# =====================================================================

NAVIGATOR_INSTRUCTIONS = (
    "You are the Navigator for a voice note-taking app. You speak out loud, so keep "
    "replies short and natural — one or two sentences, no markdown, no bullet points, "
    "no emoji. "
    "Your ONLY job is to help the user pick which note to open, or create a new one. "
    "You can see the LIST of the user's notes (each with an id, a title, and a short "
    "description) but you CANNOT see what is written inside any note — that is on "
    "purpose. Do not pretend to know a note's contents. "
    "When the user names or describes a note to open (by title, by what it's about, or "
    "by id), call open_note with their words. When they want a brand-new note, call "
    "create_note, deriving a short, specific title from what they say it's about "
    "(e.g. 'a new note about the trip' -> title 'Trip'; 'start a grocery list' -> "
    "'Groceries') — never a generic title like 'New Note' — plus a one-line description. "
    "If several notes could match, ask a brief clarifying question naming the "
    "candidates. Never read ids out loud — they are internal."
)


def _docs_for_context(docs: list) -> str:
    if not docs:
        return "The user has no notes yet. Offer to create their first one."
    lines = [f"- [{d.id}] {d.title or '(untitled)'}: {d.description or '(no description)'}"
             for d in docs]
    return "Here is the user's current list of notes (id, title, description ONLY):\n" + \
        "\n".join(lines)


class NavigatorAgent(Agent):
    """MAIN-state agent. Sees only the docs list; opens or creates docs."""

    def __init__(self, docs: list) -> None:
        super().__init__(
            instructions=NAVIGATOR_INSTRUCTIONS + "\n\n" + _docs_for_context(docs)
        )

    @function_tool
    async def open_note(self, context: RunContext[SessionMemory], query: str) -> str:
        """Resolve which note the user wants and switch the app into it.

        Args:
            query: The user's words — a title, a description, or an id.
        """
        store = context.userdata.store
        q = query.strip()
        # 1) exact id
        doc = store.get_doc(q)
        # 2) title/description search
        matches = store.search_docs(q) if not doc else []
        if not doc and len(matches) == 1:
            doc = matches[0]
        if doc:
            await send_navigate(context.userdata, doc)
            logger.info("open_note: resolved %r -> %s (%s)", q, doc.title, doc.id)
            return f"Opening {doc.title or 'that note'}."
        if len(matches) > 1:
            titles = ", ".join(m.title or "(untitled)" for m in matches[:5])
            return f"I found a few that could match: {titles}. Which one?"
        logger.info("open_note: no match for %r", q)
        return (
            f"I couldn't find a note matching '{q}'. Want me to create a new note for it?"
        )

    @function_tool
    async def create_note(
        self, context: RunContext[SessionMemory], title: str, description: str = ""
    ) -> str:
        """Create a brand-new note (doc) and switch the app into it.

        Args:
            title: A short title for the new note.
            description: A one-line description of what the note is about.
        """
        doc = context.userdata.store.create_doc(title.strip(), description.strip())
        logger.info("create_note: created %s %r", doc.id, doc.title)
        await broadcast_docs(context.userdata)
        await send_navigate(context.userdata, doc)
        return f"Created '{doc.title}'. Opening it now."


# =====================================================================
# v3.0 NOTE state — NoteAgent (context = ONE doc only; hard isolation)
# =====================================================================

NOTE_INSTRUCTIONS = (
    "You are a focused note-taking assistant for ONE specific note. You speak out "
    "loud, so keep replies short and natural — one or two sentences, no markdown, no "
    "bullet points, no emoji. "
    "Everything the user says is about THIS note. As they talk, jot things down by "
    "calling add_entry; change a line with update_entry and remove one with "
    "delete_entry; use list_entries when you need an entry's id first (never read ids "
    "aloud). "
    "Do NOT call update_doc_meta during ordinary note-taking. The title and "
    "description were set by the user and should almost never change — adding or "
    "editing entries is NOT a reason to touch them. Only call update_doc_meta if the "
    "user EXPLICITLY asks to rename the note or change its description. When in doubt, "
    "leave the title and description exactly as they are. "
    "You only know about THIS note — you have no access to the user's other notes, so "
    "never refer to them."
)


def _note_for_context(doc) -> str:
    if doc is None:
        return "This note could not be loaded."
    lines = [f"  - [{e.id}] {e.text}" for e in (doc.entries or [])]
    body = "\n".join(lines) if lines else "  (no entries yet)"
    return (
        f"You are working on this note (and ONLY this note):\n"
        f"Title: {doc.title or '(untitled)'}\n"
        f"Description: {doc.description or '(none)'}\n"
        f"Entries so far:\n{body}"
    )


class NoteAgent(Agent):
    """NOTE-state agent. Loaded with a single doc; every tool is scoped to it."""

    def __init__(self, doc) -> None:
        super().__init__(instructions=NOTE_INSTRUCTIONS + "\n\n" + _note_for_context(doc))

    @function_tool
    async def add_entry(self, context: RunContext[SessionMemory], text: str) -> str:
        """Jot a new line down in this note.

        Args:
            text: The thing to note (can be any length).
        """
        ud = context.userdata
        text = text.strip()
        if not text:
            return "There was nothing to add."
        entry = ud.store.add_entry(ud.doc_id, text)
        if entry is None:
            return "I couldn't find this note to add to."
        logger.info("add_entry[%s]: %s", ud.doc_id, entry.id)
        await broadcast_doc(ud)
        return "Added."

    @function_tool
    async def list_entries(self, context: RunContext[SessionMemory]) -> str:
        """List this note's entries WITH ids (for your internal use; never read ids aloud)."""
        ud = context.userdata
        entries = ud.store.list_entries(ud.doc_id)
        if not entries:
            return "This note has no entries yet."
        return "\n".join(f"id={e.id} | {e.text}" for e in entries)

    @function_tool
    async def update_entry(
        self, context: RunContext[SessionMemory], entry_id: str, text: str
    ) -> str:
        """Change the text of one entry in this note.

        Args:
            entry_id: The id of the entry to change (from list_entries).
            text: The new text.
        """
        ud = context.userdata
        # Isolation guard: only touch entries that belong to THIS doc.
        existing = ud.store.get_entry(entry_id.strip())
        if not existing or existing.doc_id != ud.doc_id:
            return f"I couldn't find an entry {entry_id} in this note."
        updated = ud.store.update_entry(entry_id.strip(), text.strip())
        logger.info("update_entry[%s]: %s", ud.doc_id, entry_id)
        await broadcast_doc(ud)
        return f"Updated. It now says: {updated.text}."

    @function_tool
    async def delete_entry(self, context: RunContext[SessionMemory], entry_id: str) -> str:
        """Delete one entry from this note.

        Args:
            entry_id: The id of the entry to delete (from list_entries).
        """
        ud = context.userdata
        existing = ud.store.get_entry(entry_id.strip())
        if not existing or existing.doc_id != ud.doc_id:
            return f"I couldn't find an entry {entry_id} in this note."
        ud.store.delete_entry(entry_id.strip())
        logger.info("delete_entry[%s]: %s", ud.doc_id, entry_id)
        await broadcast_doc(ud)
        return "Deleted that entry."

    @function_tool
    async def update_doc_meta(
        self, context: RunContext[SessionMemory],
        title: str = "", description: str = "",
    ) -> str:
        """Rename this note and/or change its description. Use SPARINGLY. Pass an
        EMPTY STRING for a field you want to leave unchanged.

        (Both args are plain strings with an "" default on purpose — optional/null
        params trip Groq's strict tool-call validation.)

        Args:
            title: New title, or "" to leave the title unchanged.
            description: New description, or "" to leave the description unchanged.
        """
        ud = context.userdata
        new_title = title.strip()
        new_desc = description.strip()
        if not new_title and not new_desc:
            return "Nothing to change."
        updated = ud.store.update_doc(
            ud.doc_id,
            title=new_title or None,
            description=new_desc or None,
        )
        if not updated:
            return "I couldn't find this note."
        logger.info("update_doc_meta[%s]: title=%r desc=%r", ud.doc_id, new_title, new_desc)
        await broadcast_doc(ud)
        return "Updated this note's details."


# ---- v3.0 live-sync helpers ---------------------------------------------------


async def broadcast_docs(ud: SessionMemory) -> None:
    """MAIN: publish the docs list (id/title/description) on the 'docs' topic."""
    if ud.room is None:
        return
    payload = json.dumps(
        {"type": "docs", "docs": [d.to_dict() for d in ud.store.list_docs()]}
    )
    try:
        await ud.room.local_participant.publish_data(
            payload, reliable=True, topic=DOCS_TOPIC
        )
        logger.info("broadcast_docs: published %d byte(s)", len(payload))
    except Exception:
        logger.exception("broadcast_docs failed")


async def broadcast_doc(ud: SessionMemory) -> None:
    """NOTE: publish THIS doc (with its entries) on the 'doc' topic."""
    if ud.room is None or not ud.doc_id:
        return
    doc = ud.store.get_doc_full(ud.doc_id)
    payload = json.dumps({"type": "doc", "doc": doc.to_dict() if doc else None})
    try:
        await ud.room.local_participant.publish_data(
            payload, reliable=True, topic=DOC_TOPIC
        )
        logger.info("broadcast_doc[%s]: published %d byte(s)", ud.doc_id, len(payload))
    except Exception:
        logger.exception("broadcast_doc failed")


async def send_navigate(ud: SessionMemory, doc) -> None:
    """MAIN: tell the app to transition into NOTE state for this doc id."""
    if ud.room is None:
        return
    payload = json.dumps({"type": "navigate", "doc_id": doc.id, "title": doc.title})
    try:
        await ud.room.local_participant.publish_data(
            payload, reliable=True, topic=NAVIGATE_TOPIC
        )
        logger.info("send_navigate -> %s (%s)", doc.title, doc.id)
    except Exception:
        logger.exception("send_navigate failed")


async def _apply_docs_edit(ud: SessionMemory, data: bytes) -> None:
    """MAIN: apply a typed doc edit (topic 'docs-edit'), then re-broadcast docs."""
    try:
        msg = json.loads(data.decode("utf-8"))
    except Exception:
        logger.exception("docs-edit: bad JSON")
        return
    action = msg.get("action")
    logger.info("docs-edit inbound: %s", action)
    try:
        if action == "create":
            ud.store.create_doc(msg.get("title", ""), msg.get("description", ""))
        elif action == "update":
            ud.store.update_doc(
                msg["id"], title=msg.get("title"), description=msg.get("description")
            )
        elif action == "delete":
            ud.store.delete_doc(msg["id"])
        else:
            logger.warning("docs-edit: unknown action %r", action)
            return
    except Exception:
        logger.exception("docs-edit: failed to apply %r", action)
        return
    await broadcast_docs(ud)


async def _apply_doc_edit(ud: SessionMemory, data: bytes) -> None:
    """NOTE: apply a typed entry/meta edit (topic 'doc-edit') for THIS doc only."""
    try:
        msg = json.loads(data.decode("utf-8"))
    except Exception:
        logger.exception("doc-edit: bad JSON")
        return
    action = msg.get("action")
    logger.info("doc-edit inbound[%s]: %s", ud.doc_id, action)
    try:
        if action == "add_entry":
            ud.store.add_entry(ud.doc_id, msg.get("text", ""))
        elif action == "update_entry":
            # Isolation guard: ignore entries that aren't in this doc.
            existing = ud.store.get_entry(msg["id"])
            if existing and existing.doc_id == ud.doc_id:
                ud.store.update_entry(msg["id"], msg.get("text", ""))
        elif action == "delete_entry":
            existing = ud.store.get_entry(msg["id"])
            if existing and existing.doc_id == ud.doc_id:
                ud.store.delete_entry(msg["id"])
        elif action == "update_meta":
            ud.store.update_doc(
                ud.doc_id, title=msg.get("title"), description=msg.get("description")
            )
        else:
            logger.warning("doc-edit: unknown action %r", action)
            return
    except Exception:
        logger.exception("doc-edit: failed to apply %r", action)
        return
    await broadcast_doc(ud)


# ---- entrypoint: route on room metadata ---------------------------------------


def _build_session(userdata: SessionMemory) -> AgentSession[SessionMemory]:
    """The shared Deepgram/Groq/Deepgram + Silero pipeline used by every mode."""
    return AgentSession(
        userdata=userdata,
        vad=silero.VAD.load(),
        stt=deepgram.STT(model="nova-3", language="en-US"),
        llm=groq.LLM(model="llama-3.3-70b-versatile"),
        tts=deepgram.TTS(model="aura-2-thalia-en"),
    )


async def entrypoint(ctx: JobContext) -> None:
    store = NotesStore.from_env()
    # Migrate any pre-v3.0 flat notes into a doc on first run (idempotent, no-op after).
    store.migrate_legacy_notes()

    # Route on the room metadata stamped by the token server (available at dispatch).
    meta_raw = ctx.job.room.metadata or (ctx.room.metadata if ctx.room else "") or ""
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except Exception:
        meta = {}
    mode = meta.get("mode") or "legacy"
    doc_id = meta.get("doc_id")
    logger.info("routing: room=%s mode=%s doc_id=%s", ctx.job.room.name, mode, doc_id)

    userdata = SessionMemory(store=store, room=ctx.room, mode=mode, doc_id=doc_id)
    session = _build_session(userdata)

    if mode == "main":
        await _start_main(ctx, session, userdata)
    elif mode == "note":
        await _start_note(ctx, session, userdata)
    else:
        await _start_legacy(ctx, session, userdata)


async def _start_main(ctx, session, userdata) -> None:
    docs = userdata.store.list_docs()
    logger.info("main: %d doc(s) in list", len(docs))

    def _on_data(packet: rtc.DataPacket) -> None:
        if packet.topic == DOCS_EDIT_TOPIC:
            asyncio.create_task(_apply_docs_edit(userdata, packet.data))

    # Re-push state to any (re)joining app, even if this agent was already running
    # in a lingering room (so reopening MAIN always repopulates the list).
    def _on_join(_p: rtc.RemoteParticipant) -> None:
        asyncio.create_task(broadcast_docs(userdata))

    ctx.room.on("data_received", _on_data)
    ctx.room.on("participant_connected", _on_join)
    await session.start(agent=NavigatorAgent(docs=docs), room=ctx.room)
    await broadcast_docs(userdata)
    greet = (
        "Greet the user in one short sentence and ask which note they'd like to open, "
        "or whether they want to start a new one."
    )
    if docs:
        greet = (
            f"Greet the user in one short sentence, mention they have {len(docs)} "
            "note(s), and ask which one to open or whether to start a new one."
        )
    await session.generate_reply(instructions=greet)


async def _start_note(ctx, session, userdata) -> None:
    doc = userdata.store.get_doc_full(userdata.doc_id) if userdata.doc_id else None
    logger.info(
        "note: doc_id=%s title=%r entries=%d",
        userdata.doc_id, doc.title if doc else None, len(doc.entries) if doc else 0,
    )

    def _on_data(packet: rtc.DataPacket) -> None:
        if packet.topic == DOC_EDIT_TOPIC:
            asyncio.create_task(_apply_doc_edit(userdata, packet.data))

    # Re-push this doc to any (re)joining app, even if the agent was already
    # running in a lingering note-{docId} room (so reopening a note repopulates).
    def _on_join(_p: rtc.RemoteParticipant) -> None:
        asyncio.create_task(broadcast_doc(userdata))

    ctx.room.on("data_received", _on_data)
    ctx.room.on("participant_connected", _on_join)
    await session.start(agent=NoteAgent(doc=doc), room=ctx.room)
    await broadcast_doc(userdata)
    title = doc.title if doc else "this note"
    await session.generate_reply(
        instructions=(
            f"Greet the user in one short sentence, say you're focused on '{title}', "
            "and invite them to start adding to it. Do not list the existing entries "
            "unless asked."
        )
    )


async def _start_legacy(ctx, session, userdata) -> None:
    """v2.0 behavior, preserved verbatim."""
    existing = userdata.store.list_all()
    logger.info("legacy: %d existing note(s) loaded from store", len(existing))

    def _on_data(packet: rtc.DataPacket) -> None:
        if packet.topic == NOTES_EDIT_TOPIC:
            asyncio.create_task(_apply_inbound_edit(userdata, packet.data))

    def _on_join(_p: rtc.RemoteParticipant) -> None:
        asyncio.create_task(broadcast_notes(userdata))

    ctx.room.on("data_received", _on_data)
    ctx.room.on("participant_connected", _on_join)
    await session.start(agent=MemoryAssistant(existing_notes=existing), room=ctx.room)
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
