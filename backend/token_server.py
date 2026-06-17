"""Token server for DropNote.

Auth (DropNote): minting a LiveKit token now requires a valid session. The app signs
in at ``POST /login`` (email + password, checked against the Postgres ``user_schema``
store) to get a JWT, then sends it as ``Authorization: Bearer`` on ``POST /token``.
Accounts are created out-of-band via ``POST /users`` (no UI), guarded by an admin
secret header — for personal use only. See ``auth.py`` and ``user_store.py``.

v2.0: mints a short-lived LiveKit access token for a brand-new random room on
every request (empty body) — preserved unchanged for backward compatibility.

v3.0 (additive): the request may carry a ``mode`` ("main" | "note") and, for note
mode, a ``doc_id``. The server then:
  - picks a room name that encodes the state — ``main-{rand}`` for navigation,
    ``note-{docId}`` for per-doc isolation, and
  - stamps the room's metadata (via RoomConfiguration) with {"mode", "doc_id"} so
    the agent worker can read ``ctx.room.metadata`` and construct the right agent
    (NavigatorAgent vs NoteAgent) loading only the appropriate context.
"""

from __future__ import annotations

import json
import os
import uuid

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from pydantic import BaseModel

from auth import create_access_token, require_admin, require_user
from user_store import UserStore

load_dotenv()

# Public LiveKit URL handed to the app (the LAN address a phone can reach). The
# agent container talks to LiveKit over the internal compose network instead.
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

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


class TokenRequest(BaseModel):
    # Optional: caller can suggest an identity; otherwise we generate one.
    identity: str | None = None
    # v3.0 state machine. None/"" => legacy v2.0 behavior (random room, no metadata).
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

    # Choose the room (which encodes the state) and the metadata the agent reads.
    metadata: dict | None = None
    if mode == "main":
        # Fresh main room per navigation session.
        room = f"main-{uuid.uuid4().hex[:12]}"
        metadata = {"mode": "main"}
    elif mode == "note":
        if not doc_id:
            raise HTTPException(status_code=400, detail="mode 'note' requires a doc_id.")
        # Per-doc room enforces context isolation (one room == one doc).
        room = f"note-{doc_id}"
        metadata = {"mode": "note", "doc_id": doc_id}
    else:
        # Legacy v2.0: fresh random room, no metadata, MemoryAssistant path.
        room = f"voice-memory-{uuid.uuid4().hex[:12]}"

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
    )
    if metadata is not None:
        # Stamp room metadata so the agent can route on ctx.room.metadata. The room
        # is created (with this metadata) when the participant joins.
        builder = builder.with_room_config(
            api.RoomConfiguration(name=room, metadata=json.dumps(metadata))
        )

    token = builder.to_jwt()

    return TokenResponse(
        token=token, url=LIVEKIT_URL, room=room, identity=identity,
        mode=(mode or None), doc_id=(doc_id or None),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
