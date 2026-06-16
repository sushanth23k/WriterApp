# WriterApp

Voice Memory Assistant (**v3.0**) — a hands-free voice note-taking app.

- **Backend** (`backend/`): self-hosted LiveKit SFU (Docker) + FastAPI token server + LiveKit Agents worker, with a pluggable notes store — **PostgreSQL** (Supabase) in the cloud, or an encrypted **SQLite/SQLCipher** file for local/offline dev.
- **Frontend** (`app/`): Expo React Native iOS app.

## Project structure

```
WriterApp/
├── app/                         # Expo React Native iOS app
│   └── src/config.ts            # TOKEN_SERVER_URL (local LAN vs prod domain)
├── backend/
│   ├── agent.py                 # LiveKit Agents worker (Navigator/Note/legacy)
│   ├── token_server.py          # FastAPI: POST /token, GET /health
│   ├── store.py                 # NotesStore factory → SqliteNotesStore | PgNotesStore
│   ├── migrate.py               # in-place v2.0 flat-notes → v3.0 docs migration
│   ├── migrate_to_postgres.py   # one-time SQLite → Postgres data migration
│   ├── test_store.py            # SQLite: flat-notes smoke test
│   ├── test_docs_store.py       # SQLite: doc/entry + migration + at-rest test
│   ├── test_pg_store.py         # Postgres: doc/entry CRUD + isolation (throwaway schema)
│   ├── test_v3_flow.py          # headless data-channel e2e against the running stack
│   ├── Dockerfile               # shared backend image (token server + agent)
│   ├── docker-compose.yml       # LOCAL dev stack (LAN)
│   ├── docker-compose.prod.yml  # SINGLE-VM prod stack (+ Caddy TLS, Postgres)
│   ├── livekit.yaml             # local dev SFU config (gitignored — holds a dev key)
│   ├── livekit.prod.yaml        # VM SFU config (public IP; keys via env)
│   └── Caddyfile                # TLS reverse proxy for the prod stack
└── Readme.md
```

### Notes store — two backends behind one interface

`store.py` keeps the dataclasses (`Note`, `NoteEntry`, `NoteDoc`) and public method
surface identical across backends, so `agent.py` and the data-channel JSON shape are
unaffected by which one is active. `NotesStore.from_env()` is a factory:

- **`DB_URL` (or `DB_POOL_URL`) set → `PgNotesStore`** (PostgreSQL/Supabase, psycopg3 +
  connection pool). Tables live in the `DB_SCHEMA` schema (default `writer_app`).
  "Encrypted at rest" comes from Supabase's managed AES-256 encryption, with TLS in
  transit (`DB_SSL_REQUIRE` → `sslmode=require`).
- **otherwise → `SqliteNotesStore`** (local SQLite with SQLCipher/AES-256 at rest;
  key + path from `SQLCIPHER_KEY` / `NOTES_DB_PATH`).

Migrate a local encrypted `notes.db` into Postgres once (idempotent, preserves
ids/timestamps):

```bash
cd backend
.venv/bin/python migrate_to_postgres.py
```

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
.venv/bin/python test_pg_store.py                      # Postgres backend (throwaway writer_app_test schema)
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

## Deploy to a single GCE VM (Postgres-backed)

The **entire** backend (LiveKit SFU + token server + agent) runs on **one Compute
Engine VM** via `backend/docker-compose.prod.yml` — the true "single cloud server."

> **Why not Cloud Run?** Cloud Run serves only HTTP/WS/gRPC over a single TCP port and
> has **no UDP**. The LiveKit SFU needs **UDP 7882** for WebRTC media, so the SFU can't
> live on Cloud Run. A plain VM (with UDP open) is the simplest correct home for the
> whole stack.

**1. VM + firewall.** Create an `e2-medium` (Debian/Ubuntu) VM with a **static external
IP**, install Docker + the compose plugin. Open ingress:

| Port | Proto | Why |
|---|---|---|
| 443 | tcp | https/wss via Caddy (and 80/tcp briefly for ACME) |
| 7881 | tcp | LiveKit WebRTC TCP fallback |
| 7882 | udp | LiveKit WebRTC media |
| 22 | tcp | admin |

**2. DNS + TLS.** Point an A record (e.g. `api.example.com`) at the VM's static IP. Edit
`backend/Caddyfile`, replacing `api.example.com` with your domain (and set an ACME email).
Caddy auto-provisions Let's Encrypt certs. It fans out by path on the one domain:
`/token` + `/health` → token server, everything else (incl. the LiveKit signaling
socket) → LiveKit.

**3. `backend/.env` on the VM.** Keep the existing `DB_URL` / `DB_POOL_URL` /
`DB_SSL_REQUIRE` (Postgres), and set the LiveKit values to the **public** URL/keys:

```bash
LIVEKIT_URL=wss://api.example.com          # what the token server hands the app
LIVEKIT_API_KEY=<your-key>
LIVEKIT_API_SECRET=<your-secret>           # also fed to the SFU via LIVEKIT_KEYS
```

(`livekit.prod.yaml` uses `use_external_ip: true` to advertise the VM's public IP for
media; the agent overrides `LIVEKIT_URL=ws://livekit:7880` internally via the compose
file, so it registers over the private network regardless.)

**4. Bring it up:**

```bash
cd backend
docker compose -f docker-compose.prod.yml up -d --build
```

**5. Point the app at prod.** In `app/src/config.ts` set
`TOKEN_SERVER_URL = 'https://api.example.com'`, then rebuild the app.

**Verify:**

```bash
curl https://api.example.com/health                              # {"status":"ok"}
docker compose -f docker-compose.prod.yml logs agent | grep "registered worker"
```

Then connect from the iPhone over the internet (not LAN): the MAIN list loads from
Postgres, and voice + typed CRUD persist across app restarts and redeploys.

> Out of scope: horizontal SFU scaling (a single VM is fine here; multi-node LiveKit
> needs Redis) and column-level `pgcrypto` (Supabase at-rest + TLS already cover
> "encrypted at rest").

## Housekeeping

Clear macOS AppleDouble temp files:

```bash
find . -name "._*" -type f -delete
```
