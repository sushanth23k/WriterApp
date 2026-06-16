# WriterApp

Voice Memory Assistant (**v3.0**) — a hands-free voice note-taking app.

- **Backend** (`backend/`): self-hosted LiveKit SFU (Docker) + FastAPI token server + LiveKit Agents worker, with an encrypted SQLite (SQLCipher) store.
- **Frontend** (`app/`): Expo React Native iOS app.

## What's new in v3.0

- **Note docs, not flat notes.** A note is now a *doc* (unique id + title + description) holding an arbitrary-length list of *entries*. Old v2.0 flat notes are auto-migrated into an "Imported Notes" doc on first run (non-destructive). See `backend/store.py`.
- **Two-state app.**
  - **MAIN** — a scrollable list of doc cards; a *navigator* voice agent (whose context is the docs **list only** — it can't see entry contents) opens a doc by title/id/description or creates a new one. Tap a card or speak. Start/Stop control mutes the mic without dropping the live list.
  - **NOTE** — an **isolated** conversation about one doc (its own `note-{id}` LiveKit room; the agent loads only that doc). Add/edit/delete entries and edit the title/description by voice or typing; everything live-syncs.
- **Single-command backend.** One `docker compose up` brings up all three services (see below).

> LLM: the agents use **Groq** (`llama-3.3-70b-versatile`) — the proven v2.0 pipeline (Deepgram STT/TTS + Silero VAD). Swappable to Anthropic in one place (`backend/agent.py`, `_build_session`).

## Prerequisites

- Docker (for the LiveKit server)
- Node ≥ 20.19.4 — use the keg-only Node 22 on PATH for all Expo commands:
  ```bash
  export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
  ```
- Xcode + iOS Simulator (for `expo run:ios`)
- `backend/.env` filled in (copy from `backend/.env.example`)

> The LAN IP `192.168.1.104` is hardcoded in `backend/livekit.yaml`, `backend/.env`, and `app/src/config.ts`. If your Mac's IP changes (find it with `ipconfig getifaddr en0`), update all three.

## Start the backend — one command

```bash
cd backend
find . -name '._*' -type f -delete   # see note below
docker compose up                    # add --build the first time / after code changes
```

This brings up **all three** services, wired over an internal network with a shared `backend/.env`:

| Service | Exposed URL | Purpose |
|---|---|---|
| LiveKit SFU | `ws://192.168.1.104:7880` (TCP `:7881`, media `:7882/udp`) | self-hosted media server |
| Token server | `http://192.168.1.104:8080` (`POST /token`, `GET /health`) | mints LiveKit tokens; stamps room metadata (`mode`/`doc_id`) |
| Agent worker | _(no port — outbound worker)_ | NavigatorAgent / NoteAgent / legacy MemoryAssistant |

Quick checks:

```bash
curl http://192.168.1.104:8080/health                 # {"status":"ok"}
docker compose logs agent | grep "registered worker"  # agent connected to LiveKit
.venv/bin/python test_v3_flow.py                       # headless e2e: docs/entries CRUD + isolation
```

> **macOS gotcha:** the external APFS volume auto-creates `._*` AppleDouble files that break the Docker build context (`failed to xattr … operation not permitted`). Delete them in the **same** command as the build — `.dockerignore` doesn't help (it's a context-sender failure).

### Combined single image (alternative)

A single container running both token server + agent via supervisord:

```bash
docker build -t vma-backend backend/
docker run --rm --env-file backend/.env -p 8080:8080 vma-backend   # still needs the livekit service
```

### Manual (no Docker) — the original v2.0 way still works

Run each in its own terminal from `backend/`, using the venv interpreter (deps live in `backend/.venv`):

```bash
docker compose up livekit            # just the SFU
.venv/bin/python token_server.py
.venv/bin/python agent.py dev
```

## Start the frontend (iOS Simulator)

1. **Open the Simulator window first** (so you can watch the install). `npx expo run:ios` boots a device in the background but does not always bring the GUI forward — if the window is missing this is why:
   ```bash
   open -a Simulator
   ```
   If no device is booted, boot one first, then open:
   ```bash
   xcrun simctl boot "iPhone 17 Pro"
   open -a Simulator
   ```

2. **Build & install the app onto the simulator:**
   ```bash
   export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
   cd app
   npx expo run:ios
   ```

Useful checks:

```bash
xcrun simctl list devices booted     # which device(s) are running
xcrun simctl list devices available  # all installable simulators
```

## Housekeeping

Clear macOS AppleDouble temp files:

```bash
find . -name "._*" -type f -delete
```
