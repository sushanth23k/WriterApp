"""Token server for Voice Memory Assistant.

Mints a short-lived LiveKit access token for a brand-new random room on every
request. A fresh room per request is what enforces single-conversation memory:
the agent that joins the new room gets a fresh in-memory session.
"""

from __future__ import annotations

import os
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from pydantic import BaseModel

load_dotenv()

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

app = FastAPI(title="Voice Memory Assistant Token Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TokenRequest(BaseModel):
    # Optional: caller can suggest an identity; otherwise we generate one.
    identity: str | None = None


class TokenResponse(BaseModel):
    token: str
    url: str
    room: str
    identity: str


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/token", response_model=TokenResponse)
async def create_token(req: TokenRequest | None = None) -> TokenResponse:
    if not (LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
        raise HTTPException(
            status_code=500,
            detail="Server missing LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET.",
        )

    # Fresh random room per request => fresh single-conversation memory.
    room = f"voice-memory-{uuid.uuid4().hex[:12]}"
    identity = (req.identity if req and req.identity else None) or f"user-{uuid.uuid4().hex[:8]}"

    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )

    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
        .to_jwt()
    )

    return TokenResponse(token=token, url=LIVEKIT_URL, room=room, identity=identity)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
