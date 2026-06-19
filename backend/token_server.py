"""Token server + notes REST API for DropNote.

Auth: minting a LiveKit token requires a valid session. The app signs in at
``POST /login`` (email + password, checked against the Postgres ``user_schema`` store)
to get a JWT, then sends it as ``Authorization: Bearer`` on every protected call.
Accounts are created out-of-band via ``POST /users`` (no UI), guarded by an admin
secret header — for personal use only. See ``auth.py`` and ``user_store.py``.

LiveKit token (``POST /token``): the request carries a ``mode`` ("main" | "note") and,
for note mode, a ``doc_id``. The server picks a room name that encodes the state
(``main-{rand}`` for navigation, ``note-{docId}`` for per-doc isolation) and stamps the
room metadata (via RoomConfiguration) with {"mode", "doc_id", "user"} so the agent
worker can route to the right agent AND scope every note operation to that user.

Notes REST API (``/notes`` …): the app reads and writes its notes DIRECTLY here — it
never depends on the LiveKit background data channel just to see its notes. Every
endpoint is gated by the Bearer JWT and scoped to the signed-in user, so each account
only ever touches its own notes (stored in the ``writer_app`` schema; see
``notes_store.py``).
"""

from __future__ import annotations

import json
import logging
import uuid

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from pydantic import BaseModel

import env_util
from auth import create_access_token, require_admin, require_user
from notes_store import NotesStore
from user_store import UserStore

load_dotenv()

logger = logging.getLogger("dropnote-token-server")

# Public LiveKit URL handed to the app (the LAN address a phone can reach). The
# agent container talks to LiveKit over the internal compose network instead. Read
# through env_util so stray quotes/whitespace (e.g. from a compose env_file) are
# tolerated rather than silently breaking the value.
LIVEKIT_URL = env_util.get("LIVEKIT_URL")
LIVEKIT_API_KEY = env_util.get("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = env_util.get("LIVEKIT_API_SECRET")

app = FastAPI(title="DropNote Token Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lazily-opened Postgres user store (auth lives in the `user_schema` schema). Opened
# on first use so the token server still imports/health-checks without a DB present.
_user_store: UserStore | None = None


def get_user_store() -> UserStore:
    global _user_store
    if _user_store is None:
        _user_store = UserStore.from_env()
    return _user_store


# Lazily-opened Postgres notes store (notes live in the `writer_app` schema, scoped
# per user). Opened on first use, mirroring the user store above.
_notes_store: NotesStore | None = None


def get_notes_store() -> NotesStore:
    global _notes_store
    if _notes_store is None:
        _notes_store = NotesStore.from_env()
    return _notes_store


class TokenRequest(BaseModel):
    # Optional: caller can suggest an identity; otherwise we generate one.
    identity: str | None = None
    # State machine. None/"" or "main" => navigator room; "note" => per-doc room.
    mode: str | None = None
    # Required when mode == "note": which doc this isolated conversation is about.
    doc_id: str | None = None


class TokenResponse(BaseModel):
    token: str
    url: str
    room: str
    identity: str
    mode: str | None = None
    doc_id: str | None = None


class Credentials(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str


class CreatedUser(BaseModel):
    email: str


# Vars the token server needs to actually function (LiveKit minting + Postgres auth).
REQUIRED_ENV = (
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "DATABASE_URL",
    "AUTH_JWT_SECRET",
)


@app.on_event("startup")
async def _check_env() -> None:
    """Log a single, loud, actionable line if required config is missing.

    Stores/secrets are opened lazily (so /health still answers for diagnosis), which
    means a missing var would otherwise only surface as a 500 deep inside a request.
    Surfacing it at startup makes a misconfigured deploy obvious in the VM serial log.
    """
    missing = [n for n in REQUIRED_ENV if not env_util.get(n)]
    if missing:
        logger.error(
            "CONFIG ERROR — missing required environment variable(s): %s. "
            "The server is up but auth/token minting will fail until these are set "
            "in backend/.env (shipped to the VM at deploy time).",
            ", ".join(missing),
        )
    else:
        logger.info("env check ok — all required variables present")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/login", response_model=LoginResponse)
async def login(creds: Credentials) -> LoginResponse:
    """Exchange email + password for a session JWT."""
    store = get_user_store()
    email = creds.email.strip().lower()
    if not store.verify_user(email, creds.password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return LoginResponse(access_token=create_access_token(email), email=email)


@app.post("/users", response_model=CreatedUser, status_code=201)
async def create_user(
    creds: Credentials, _: None = Depends(require_admin)
) -> CreatedUser:
    """Create a user account (no UI — personal use, guarded by X-Admin-Token)."""
    store = get_user_store()
    try:
        account = store.create_user(creds.email, creds.password)
    except ValueError as e:
        # Duplicate email or invalid input.
        status_code = 409 if "already exists" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    return CreatedUser(email=account.email)


@app.post("/token", response_model=TokenResponse)
async def create_token(
    req: TokenRequest | None = None, user: str = Depends(require_user)
) -> TokenResponse:
    if not (LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
        raise HTTPException(
            status_code=500,
            detail="Server missing LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET.",
        )

    mode = (req.mode if req and req.mode else "").strip().lower()
    doc_id = (req.doc_id if req and req.doc_id else "").strip()

    # Choose the room (which encodes the state) and the metadata the agent reads. The
    # authenticated user's email is always stamped so the worker scopes notes to them.
    if mode == "note":
        if not doc_id:
            raise HTTPException(status_code=400, detail="mode 'note' requires a doc_id.")
        # Per-doc room enforces context isolation (one room == one doc).
        room = f"note-{doc_id}"
        metadata = {"mode": "note", "doc_id": doc_id, "user": user}
    else:
        # Default/main: a fresh navigator room per session.
        room = f"main-{uuid.uuid4().hex[:12]}"
        metadata = {"mode": "main", "user": user}

    # Identity is derived from the authenticated user (unique per LiveKit room join).
    identity = f"{user}-{uuid.uuid4().hex[:6]}"

    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )

    builder = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
        # Stamp room metadata so the agent can route on ctx.room.metadata (and scope
        # notes to `user`). The room is created (with this metadata) on participant join.
        .with_room_config(
            api.RoomConfiguration(name=room, metadata=json.dumps(metadata))
        )
    )

    token = builder.to_jwt()

    return TokenResponse(
        token=token, url=LIVEKIT_URL, room=room, identity=identity,
        mode=(mode or None), doc_id=(doc_id or None),
    )


# ============================================================================
# Notes REST API — direct, per-user store/get (no LiveKit background channel).
#
# Every endpoint is gated by `require_user` (the Bearer JWT), and the resolved email
# scopes the query, so a user can only ever touch their OWN notes (writer_app schema).
# ============================================================================


class DocIn(BaseModel):
    title: str = ""
    description: str = ""


class DocPatch(BaseModel):
    title: str | None = None
    description: str | None = None


class EntryIn(BaseModel):
    text: str


class EntryPatch(BaseModel):
    text: str


@app.get("/notes")
async def list_docs(user: str = Depends(require_user)) -> list[dict]:
    """List the signed-in user's docs (id/title/description, no entries)."""
    store = get_notes_store()
    return [d.to_dict() for d in store.list_docs(user)]


@app.post("/notes", status_code=201)
async def create_doc(body: DocIn, user: str = Depends(require_user)) -> dict:
    """Create a new doc owned by the signed-in user."""
    store = get_notes_store()
    doc = store.create_doc(user, body.title, body.description)
    return doc.to_dict()


@app.get("/notes/{doc_id}")
async def get_doc(doc_id: str, user: str = Depends(require_user)) -> dict:
    """Fetch one doc WITH its entries (404 if it isn't the user's)."""
    store = get_notes_store()
    doc = store.get_doc_full(user, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Note not found.")
    return doc.to_dict()


@app.patch("/notes/{doc_id}")
async def update_doc(
    doc_id: str, body: DocPatch, user: str = Depends(require_user)
) -> dict:
    """Update a doc's title and/or description."""
    store = get_notes_store()
    doc = store.update_doc(user, doc_id, title=body.title, description=body.description)
    if doc is None:
        raise HTTPException(status_code=404, detail="Note not found.")
    return doc.to_dict()


@app.delete("/notes/{doc_id}", status_code=204)
async def delete_doc(doc_id: str, user: str = Depends(require_user)) -> None:
    """Delete a doc and all of its entries."""
    store = get_notes_store()
    if not store.delete_doc(user, doc_id):
        raise HTTPException(status_code=404, detail="Note not found.")


@app.post("/notes/{doc_id}/entries", status_code=201)
async def add_entry(
    doc_id: str, body: EntryIn, user: str = Depends(require_user)
) -> dict:
    """Add an entry to one of the user's docs."""
    store = get_notes_store()
    entry = store.add_entry(user, doc_id, body.text)
    if entry is None:
        raise HTTPException(status_code=404, detail="Note not found.")
    return entry.to_dict()


@app.patch("/notes/{doc_id}/entries/{entry_id}")
async def update_entry(
    doc_id: str, entry_id: str, body: EntryPatch, user: str = Depends(require_user)
) -> dict:
    """Update one entry's text (the entry must belong to the user's doc)."""
    store = get_notes_store()
    existing = store.get_entry(user, entry_id)
    if existing is None or existing.doc_id != doc_id:
        raise HTTPException(status_code=404, detail="Entry not found.")
    entry = store.update_entry(user, entry_id, body.text)
    return entry.to_dict()


@app.delete("/notes/{doc_id}/entries/{entry_id}", status_code=204)
async def delete_entry(
    doc_id: str, entry_id: str, user: str = Depends(require_user)
) -> None:
    """Delete one entry from the user's doc."""
    store = get_notes_store()
    existing = store.get_entry(user, entry_id)
    if existing is None or existing.doc_id != doc_id:
        raise HTTPException(status_code=404, detail="Entry not found.")
    store.delete_entry(user, entry_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
