"""DropNote — LiveKit Agent worker.

Pipeline: Deepgram STT (nova-3) -> Groq LLM (llama-3.3-70b-versatile) -> Deepgram
Aura TTS, with Silero VAD for turn detection / barge-in.

Notes are persisted PER USER in Postgres (Supabase) under the ``writer_app`` schema
via ``notes_store.py``. The signed-in user's email is stamped into the room metadata
by the token server, so the worker loads and mutates only that user's notes. Voice
mutations are broadcast to the room (so the app's view updates live during a call);
the app also reads/writes notes directly over REST (see token_server.py), so it never
depends on this background channel just to see its notes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import AsyncIterable

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    ModelSettings,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import deepgram, groq, silero

from notes_store import NotesStore, UserNotesStore

load_dotenv()

logger = logging.getLogger("dropnote-agent")

# --- v3.0 MAIN-state topics (navigator over the docs LIST) ---
DOCS_TOPIC = "docs"            # agent -> app: full docs list (id/title/description)
DOCS_EDIT_TOPIC = "docs-edit"  # app -> agent: typed doc CRUD (create/update/delete)
NAVIGATE_TOPIC = "navigate"    # agent -> app: transition into NOTE state for a doc id

# --- v3.0 NOTE-state topics (one isolated doc) ---
DOC_TOPIC = "doc"              # agent -> app: the single current doc (with entries)
DOC_EDIT_TOPIC = "doc-edit"    # app -> agent: typed entry/meta CRUD for THIS doc

# --- voice state-movement control (agent -> app) ---
# Lets the user move between states by voice: "go back" (NOTE -> MAIN) and
# "stop it" (end the conversation / disconnect). Carries {"action": ...}.
CONTROL_TOPIC = "control"


@dataclass
class SessionMemory:
    """Per-session handles. The notes themselves live in the per-user Postgres store.

    ``store`` is already bound to the signed-in user's email, so every operation is
    scoped to that user. ``mode`` selects which agent is running: "main" (NavigatorAgent
    over the docs list) or "note" (NoteAgent on one doc). ``doc_id`` is only set in
    "note" mode and scopes every operation to that doc.
    """

    store: UserNotesStore
    room: rtc.Room | None = None
    mode: str = "main"
    doc_id: str | None = None


# Shared guardrail appended to EVERY agent's instructions. The model's text is read
# aloud by TTS, so it must never verbalize any tool/function-call machinery — that's
# what causes spoken gibberish like "function add_entry(...)".
SPEAK_NATURALLY = (
    "CRITICAL — you are spoken aloud by a text-to-speech voice. Speak ONLY plain, "
    "natural, human language. NEVER say the word 'function'. Never speak, spell, or "
    "describe any tool or function name, parameter, argument, JSON, bracket, or code, "
    "and never read out internal ids. To do something, silently call the tool — do not "
    "announce, narrate, or echo the call — and THEN say one short, natural sentence "
    "about the result (e.g. 'Added that.'). Tool calls and your spoken words must never "
    "be mixed together or overlap."
)

# Appended to EVERY agent. The single biggest conversation bug is the model SAYING it
# will do something (open a note, go back, add a line) without actually emitting the
# matching tool call — so the app never moves and nothing is saved. Make the contract
# explicit: words describing an action must be backed by the tool call in the same turn.
ACT_DONT_PROMISE = (
    "ACT, DON'T PROMISE. If you tell the user you are opening a note, creating one, "
    "going back, stopping, adding, changing, or removing something, you MUST make the "
    "matching tool call in that SAME turn. Never say you did or will do something "
    "without actually calling the tool, and never just promise to do it 'next' — do it "
    "now. If you cannot do it, say so plainly instead of pretending."
)


# ---------------------------------------------------------------------------
# TTS / transcript safety net.
#
# The instructions above tell the model to speak plainly, but llama-3.3-70b still
# occasionally leaks tool/code machinery into its spoken text (e.g. "function
# add_entry(...)", snake_case ids, stray semicolons). The voice reads whatever text
# it is handed, so we scrub that text in the pipeline BEFORE it reaches TTS and the
# on-screen transcript. This is a deterministic backstop, independent of the model.
# ---------------------------------------------------------------------------

_RE_FENCE = re.compile(r"```.*?```", re.S)        # fenced code blocks
_RE_INLINE = re.compile(r"`[^`]*`")               # `inline code`
_RE_CALL = re.compile(r"\b[A-Za-z_]\w*\([^)]*\)")  # foo(...) / add_entry(milk)
_RE_FUNCWORD = re.compile(r"\bfunctions?\b", re.I)  # the literal word "function"
_RE_UNDERSCORE = re.compile(r"(?<=\w)_(?=\w)")     # snake_case joiner -> space
_RE_CODEPUNCT = re.compile(r"[{}\[\]<>`|\\]")      # punctuation TTS reads as noise
_RE_SEMI = re.compile(r"\s*;\s*")                  # semicolons -> sentence break
_RE_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,!?])")  # " ." -> "."
_RE_DUP_PUNCT = re.compile(r"([.,!?])(?:\s*[.,!?])+")  # ".." / ". ." -> "."
_RE_WS = re.compile(r"\s{2,}")


def _spoken_text(text: str) -> str:
    """Strip code/tool machinery from text so only plain language is spoken/shown.

    Conservative on purpose: it removes call-shaped tokens and code punctuation but
    leaves normal parentheticals (e.g. "milk (oat)") and ordinary words untouched.
    """
    t = _RE_FENCE.sub(" ", text)
    t = _RE_INLINE.sub(" ", t)
    t = _RE_CALL.sub(" ", t)
    t = _RE_FUNCWORD.sub(" ", t)
    t = _RE_UNDERSCORE.sub(" ", t)
    t = _RE_CODEPUNCT.sub(" ", t)
    t = _RE_SEMI.sub(". ", t)
    t = _RE_SPACE_BEFORE_PUNCT.sub(r"\1", t)
    t = _RE_DUP_PUNCT.sub(r"\1", t)
    return _RE_WS.sub(" ", t).strip()


async def _sanitized_stream(text: AsyncIterable[str]) -> AsyncIterable[str]:
    """Buffer one spoken segment, scrub it, then emit it. Replies are 1–2 sentences,
    so buffering the whole segment (rather than risking code tokens split across
    streamed chunks) costs no meaningful latency and makes the scrub reliable."""
    buf = ""
    async for chunk in text:
        buf += chunk
    cleaned = _spoken_text(buf)
    if cleaned:
        yield cleaned


class SpokenAgent(Agent):
    """Base agent whose spoken + transcribed text is guaranteed plain language.

    Both the MAIN and NOTE agents extend this so the TTS sanitizer applies everywhere.
    """

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ):
        async for frame in Agent.default.tts_node(
            self, _sanitized_stream(text), model_settings
        ):
            yield frame

    async def transcription_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ):
        async for seg in Agent.default.transcription_node(
            self, _sanitized_stream(text), model_settings
        ):
            yield seg


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
    "candidates. Never read ids out loud — they are internal. "
    "Do NOT tell the user you're opening or creating a note unless you are calling "
    "open_note or create_note in the same turn — that is the whole point of this state. "
    "If the user wants to end the conversation (e.g. 'stop it', 'we're done', "
    "'end'), call stop_conversation. "
    + ACT_DONT_PROMISE + " " + SPEAK_NATURALLY
)


# Filler words a user wraps around the actual note name ("open my X note please").
# Stripped before the per-word title/description search so they don't drag in noise.
_OPEN_STOPWORDS = {
    "open", "go", "to", "the", "a", "an", "my", "note", "notes", "please",
    "about", "for", "called", "named", "list", "show", "me", "into", "in", "on",
}


def _search_by_words(store, query: str) -> list:
    """Fallback resolver: search each meaningful word in the query and union matches
    (deduped by id), so a loosely-phrased request still resolves to a note."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", query.lower())
             if w not in _OPEN_STOPWORDS and len(w) > 1]
    seen: dict[str, object] = {}
    for w in words:
        for d in store.search_docs(w):
            seen.setdefault(d.id, d)
    return list(seen.values())


def _docs_for_context(docs: list) -> str:
    if not docs:
        return "The user has no notes yet. Offer to create their first one."
    lines = [f"- [{d.id}] {d.title or '(untitled)'}: {d.description or '(no description)'}"
             for d in docs]
    return "Here is the user's current list of notes (id, title, description ONLY):\n" + \
        "\n".join(lines)


class NavigatorAgent(SpokenAgent):
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
        # 2) title/description search on the whole phrase
        matches = store.search_docs(q) if not doc else []
        # 3) token fallback: the user rarely says the title verbatim ("open my grocery
        #    note" won't substring-match "Groceries"), so if the whole phrase missed,
        #    search each meaningful word and union the hits before giving up.
        if not doc and not matches:
            matches = _search_by_words(store, q)
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

    @function_tool
    async def stop_conversation(self, context: RunContext[SessionMemory]) -> str:
        """Stop listening — pause the microphone, staying on this page.

        Call this when the user says things like "stop it", "stop listening",
        "we're done", or "end". The page stays open; they can tap Start to resume.
        """
        await send_control(context.userdata, "stop")
        logger.info("stop_conversation[main]")
        return "Okay, I'll stop listening. Tap start when you want to talk again."


# =====================================================================
# v3.0 NOTE state — NoteAgent (context = ONE doc only; hard isolation)
# =====================================================================

NOTE_INSTRUCTIONS = (
    "You are a focused note-taking assistant for ONE specific note. You speak out "
    "loud, so keep replies short and natural — one or two sentences, no markdown, no "
    "bullet points, no emoji. "
    "Everything the user says is about THIS note. As they talk, jot things down by "
    "calling add_entry; change a line with update_entry and remove one with "
    "delete_entry. "
    "Your in-context list of entries can fall behind after a few edits, so ALWAYS call "
    "list_entries to get the CURRENT text and ids immediately before you update_entry "
    "or delete_entry, and act on exactly what it returns — never guess or reuse an old "
    "id, and never read ids aloud. "
    "Keep this note clean, concise, and non-redundant. Before adding something, check "
    "whether it is already captured: if a point already exists, EXTEND or update that "
    "entry instead of adding a duplicate or near-duplicate, and merge closely related "
    "details into ONE clear, self-contained entry rather than scattering fragments. "
    "Each entry should be a single concise point. "
    "PRESERVE what's already there: adding or editing one point must never drop or "
    "rewrite the others. Only delete an entry when the user clearly asks to remove that "
    "specific thing — when in doubt, add or update, never delete. "
    "Keep the note's title and description ACCURATE as its content grows. Whenever you "
    "add or change entries in a way that DRASTICALLY shifts or clarifies what this note "
    "is about, call update_doc_meta to refresh the title and/or description so they "
    "still summarize the note's current contents — a short, specific title and a "
    "one-line description. Do not churn them for small additions that don't change the "
    "gist, and honor a title or description the user set explicitly unless the content "
    "clearly outgrows it. Do this quietly: update the meta, then just keep helping — "
    "don't read the new title or description aloud unless asked. "
    "You only know about THIS note — you have no access to the user's other notes, so "
    "never refer to them. "
    "If the user wants to leave this note and return to their list (e.g. 'go back', "
    "'take me back', 'back to my notes'), call go_back. If they want to end the "
    "conversation entirely (e.g. 'stop it', 'we're done', 'end'), call "
    "stop_conversation. "
    + ACT_DONT_PROMISE + " " + SPEAK_NATURALLY
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


class NoteAgent(SpokenAgent):
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
        # Backstop against blatant duplicates: if this exact line already exists,
        # don't add it again (the instructions also steer the model toward merging).
        norm = text.casefold()
        if any(e.text.strip().casefold() == norm for e in ud.store.list_entries(ud.doc_id)):
            logger.info("add_entry[%s]: skipped duplicate %r", ud.doc_id, text)
            return "That's already in this note."
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
        """Refresh this note's title and/or description so they stay accurate as its
        content grows. Call this when added/changed entries drastically shift or clarify
        what the note is about. Pass an EMPTY STRING for a field to leave it unchanged.

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

    @function_tool
    async def go_back(self, context: RunContext[SessionMemory]) -> str:
        """Leave this note and return to the main notes list.

        Call this when the user says things like "go back", "take me back", or
        "back to my notes".
        """
        await send_control(context.userdata, "go_back")
        logger.info("go_back[%s]", context.userdata.doc_id)
        return "Taking you back to your notes."

    @function_tool
    async def stop_conversation(self, context: RunContext[SessionMemory]) -> str:
        """Stop listening — pause the microphone, staying on this page.

        Call this when the user says things like "stop it", "stop listening",
        "we're done", or "end". The page stays open; they can tap Start to resume.
        """
        await send_control(context.userdata, "stop")
        logger.info("stop_conversation[%s]", context.userdata.doc_id)
        return "Okay, I'll stop listening. Tap start when you want to talk again."


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


async def send_control(ud: SessionMemory, action: str) -> None:
    """Tell the app to move app state by voice: 'go_back' or 'stop'."""
    if ud.room is None:
        return
    payload = json.dumps({"type": "control", "action": action})
    try:
        await ud.room.local_participant.publish_data(
            payload, reliable=True, topic=CONTROL_TOPIC
        )
        logger.info("send_control -> %s", action)
    except Exception:
        logger.exception("send_control failed")


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


def _build_session(
    userdata: SessionMemory, engine: str = "cloud"
) -> AgentSession[SessionMemory]:
    """Build the STT->LLM->TTS + Silero VAD pipeline for the chosen engine.

    ``cloud`` (default): Deepgram STT -> Groq llama-3.3-70b -> Deepgram TTS.
    ``hybrid``: on-device MLX Whisper + Kokoro for audio, but Groq llama-3.3-70b for the
    LLM — keeps the audio private while relying on the 70B for the reliable tool-calling
    the whole app is built on.

    The MLX audio models load lazily on first use and stay resident in the worker; the
    local plugins are imported here, not at module top, so a cloud-only deploy without
    MLX installed still runs.
    """
    if engine == "hybrid":
        # On-device STT + TTS, cloud (Groq) LLM for dependable tool-calling. STT picks
        # MLX on macOS and faster-whisper on Linux (Docker/GCP) — see stt/__init__.py.
        from stt import make_local_stt
        from tts.kokoro_tts import KokoroTTS

        logger.info("building HYBRID session (on-device audio, Groq LLM)")
        return AgentSession(
            userdata=userdata,
            vad=silero.VAD.load(),
            stt=make_local_stt(),
            llm=groq.LLM(model="llama-3.3-70b-versatile"),
            tts=KokoroTTS(),
        )

    logger.info("building CLOUD (Deepgram/Groq) session")
    return AgentSession(
        userdata=userdata,
        vad=silero.VAD.load(),
        stt=deepgram.STT(model="nova-3", language="en-US"),
        llm=groq.LLM(model="llama-3.3-70b-versatile"),
        tts=deepgram.TTS(model="aura-2-thalia-en"),
    )


async def entrypoint(ctx: JobContext) -> None:
    # Route on the room metadata stamped by the token server (available at dispatch).
    meta_raw = ctx.job.room.metadata or (ctx.room.metadata if ctx.room else "") or ""
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except Exception:
        meta = {}
    user_email = (meta.get("user") or "").strip().lower()
    mode = meta.get("mode") or "main"
    doc_id = meta.get("doc_id")
    # Which inference stack to run: "local" (on-device MLX) or "cloud" (Deepgram/Groq).
    # Stamped by the token server from the app's toggle; default cloud if absent.
    engine = (meta.get("engine") or "cloud").strip().lower()
    if engine not in ("cloud", "hybrid"):
        engine = "cloud"
    logger.info(
        "routing: room=%s user=%s mode=%s doc_id=%s engine=%s",
        ctx.job.room.name, user_email, mode, doc_id, engine,
    )

    if not user_email:
        # No authenticated user on the room — nothing to scope notes to; bail out.
        logger.error("no user in room metadata; refusing to start an unscoped session")
        return

    # The store is bound to this user so every note operation is scoped to them.
    store = NotesStore.from_env().for_user(user_email)
    userdata = SessionMemory(store=store, room=ctx.room, mode=mode, doc_id=doc_id)
    session = _build_session(userdata, engine)

    if mode == "note":
        await _start_note(ctx, session, userdata)
    else:
        await _start_main(ctx, session, userdata)


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


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
