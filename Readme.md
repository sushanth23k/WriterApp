# DropNote (v3.0)

A hands-free, voice-driven note-taking app. You talk; an AI assistant organizes what you
say into **notes** and jots down **entries** inside them — and everything you can do by
voice, you can also do by typing, with the UI updating live as the assistant speaks.

Access is gated by a login (email + password). Sessions are JWT-based; user accounts live
in a PostgreSQL `user_schema` schema and are created out-of-band via an admin-only API
(no sign-up UI). See **Authentication** below.

- **Backend** (`backend/`): a self-hosted LiveKit SFU (Docker) + FastAPI token server +
  LiveKit Agents worker, with an encrypted SQLite (SQLCipher) store. Brought up with a
  single `docker compose up`.
- **Frontend** (`app/`): an Expo / React Native iOS app.

Only the AI services are remote: **Groq** (LLM), **Deepgram** (STT + TTS). Everything else
runs locally.

---

## How it works

```
  iPhone app  ──WebRTC audio + data channel──►  LiveKit SFU (self-hosted, Docker)
      │                                               ▲
      │  POST /token (mode, doc_id)                   │  agent joins the room
      ▼                                               │
  Token server (FastAPI)  ──mints token w/ room metadata──►  Agent worker (LiveKit Agents)
                                                              │  Deepgram STT → Groq LLM
                                                              │  → Deepgram TTS, Silero VAD
                                                              ▼
                                                       Encrypted SQLite store (SQLCipher)
```

The voice pipeline is **Deepgram STT (nova-3) → Groq LLM (`llama-3.3-70b-versatile`) →
Deepgram Aura TTS**, with **Silero VAD** for turn-taking / barge-in.

### Two-state app

The app is a state machine with a clear mechanism to move between two states:

- **MAIN** — browse / navigate / create. A scrollable list of note cards (title + a short
  description). A *navigator* voice agent whose context is the **docs list only** (it can't
  read entry contents) resolves a note by **title, id, or description**, or creates a new
  one — by voice or by tapping a card. When it resolves/creates a note it sends a `navigate`
  message and the app transitions into NOTE state. A **Start/Stop** control mutes the mic
  without dropping the live list.
- **NOTE** — an **isolated** conversation about a single note. The note conversation has its
  own LiveKit room (`note-{docId}`) and the agent loads **only that note** (title,
  description, entries) — it cannot see any other note. You just talk and it notes things
  down; tools cover `add/update/delete/list` entries and (used sparingly) `update_doc_meta`.

### Data model (`note docs` + `entries`)

A note is a **doc** with a unique id, a title, and a description; it holds an
arbitrary-length list of **entries**.

```
note_docs(id, title, description, created_at, updated_at)
note_entries(id, doc_id, text, created_at)        # doc_id → note_docs.id, cascades
notes(id, text, tags, created_at, updated_at)     # legacy v2.0 flat notes (kept)
```

The store is **encrypted at rest** with SQLCipher (AES-256); the key comes from
`SQLCIPHER_KEY` and is never hardcoded. On first run, any pre-existing v2.0 flat notes are
auto-migrated into an "Imported Notes" doc (non-destructive — see `backend/store.py`).

### Live-sync data-channel contract

| Topic | Direction | State | Payload |
|---|---|---|---|
| `docs` | agent → app | MAIN | full docs list (id/title/description) |
| `docs-edit` | app → agent | MAIN | typed doc CRUD (`create`/`update`/`delete`) |
| `navigate` | agent → app | MAIN | `{doc_id, title}` — transition into NOTE |
| `doc` | agent → app | NOTE | the single current doc, **with** its entries |
| `doc-edit` | app → agent | NOTE | typed entry/meta CRUD (`add_entry`/`update_entry`/`delete_entry`/`update_meta`) |
| `notes` / `notes-edit` | both | legacy | v2.0 flat-notes sync (preserved) |

The token server stamps each room's metadata with `{mode, doc_id}`; the agent reads
`ctx.job.room.metadata` to construct the right agent (`NavigatorAgent` vs `NoteAgent`, or the
legacy `MemoryAssistant` when there's no metadata).

---

## Project structure

```
WriterApp/
├── Readme.md                  ← this file
├── backend/
│   ├── docker-compose.yml     # single-command stack: livekit + token-server + agent
│   ├── Dockerfile             # shared image for token server + agent (+ supervisord)
│   ├── supervisord.conf       # combined single-image alternative (both in one container)
│   ├── requirements.txt       # livekit-agents[deepgram,groq,silero], fastapi, sqlcipher3
│   ├── .env.example           # copy to .env and fill in (never committed)
│   ├── livekit.yaml           # local LiveKit server config (gitignored — holds dev secret)
│   ├── store.py               # SQLCipher store: note_docs + note_entries (+ legacy notes)
│   ├── token_server.py        # FastAPI: mints tokens, sets room mode/doc_id metadata
│   ├── agent.py               # NavigatorAgent / NoteAgent / legacy MemoryAssistant
│   ├── migrate.py             # one-shot: fold legacy flat notes into a doc
│   ├── test_store.py          # v2.0 store smoke test (encrypted-at-rest)
│   ├── test_docs_store.py     # v3.0 doc/entry CRUD + migration + isolation guards
│   ├── test_notes_edit.py     # headless legacy round-trip over the data channel
│   ├── test_v3_flow.py        # headless v3.0 e2e: docs/entry CRUD + context isolation
│   └── devclient/index.html   # tiny browser test client
└── app/
    ├── App.tsx                # root: MAIN/NOTE state machine + audio session + LiveKitRoom
    ├── app.json               # Expo config (mic permission, bundle id)
    ├── package.json           # Expo 56, React 19, RN 0.85, @livekit/react-native 2.11
    └── src/
        ├── config.ts          # TOKEN_SERVER_URL (LAN IP)
        ├── api.ts             # fetchToken(mode, docId)
        ├── types.ts           # Doc/Entry/Screen types + data-channel TOPICS
        ├── theme.ts           # dark theme tokens (colors / spacing / type scale)
        ├── livekit.ts         # shared hooks: transcript, data topics, publish, mic, conn
        ├── ui.tsx             # StatusPill, MicButton (Start/Stop), TranscriptView
        ├── MainScreen.tsx     # MAIN: doc cards, create, voice/tap navigation
        └── NoteScreen.tsx     # NOTE: editable title/desc + live entries + per-entry CRUD
```

---

## Prerequisites

- **Docker** (Docker Desktop on macOS) for the backend stack.
- **Node ≥ 20.19.4** — use the keg-only Node 22 on PATH for all Expo commands:
  ```bash
  export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
  ```
- **Xcode** + a physical iPhone or the iOS Simulator for the app.
- `backend/.env` filled in (copy from `backend/.env.example`).

> The LAN IP `192.168.1.104` is hardcoded in `backend/livekit.yaml`, `backend/.env`, and
> `app/src/config.ts`. If your Mac's IP changes (find it with `ipconfig getifaddr en0`),
> update all three.

---

## Start the backend — one command

```bash
cd backend
find . -name '._*' -type f -delete    # see the macOS note below
docker compose up                     # add --build the first time / after code changes
```

This brings up **all three** services, wired over an internal network with a shared
`backend/.env`:

| Service | Exposed URL | Purpose |
|---|---|---|
| LiveKit SFU | `ws://192.168.1.104:7880` (TCP `:7881`, media `:7882/udp`) | self-hosted media server |
| Token server | `http://192.168.1.104:8080` (`POST /token`, `GET /health`) | mints tokens; sets room `mode`/`doc_id` |
| Agent worker | _(no port — outbound worker)_ | NavigatorAgent / NoteAgent / legacy |

Quick checks:

```bash
curl http://192.168.1.104:8080/health                 # {"status":"ok"}
docker compose logs agent | grep "registered worker"  # agent connected to LiveKit
```

> **macOS gotcha:** the external APFS volume auto-creates `._*` AppleDouble files that break
> the Docker build context (`failed to xattr … operation not permitted`). Delete them in the
> **same** command as the build — `.dockerignore` does not help (it's a context-sender
> failure that happens before ignore filtering).

### Alternatives

- **Combined single image** (token server + agent in one container via supervisord):
  ```bash
  docker build -t vma-backend backend/
  docker run --rm --env-file backend/.env -p 8080:8080 vma-backend   # still needs livekit
  ```
- **Manual (no Docker)** — the original v2.0 way still works; run each in its own terminal
  from `backend/` using the venv interpreter (deps live in `backend/.venv`):
  ```bash
  docker compose up livekit            # just the SFU
  .venv/bin/python token_server.py
  .venv/bin/python agent.py dev
  ```

---

## Start the frontend (iOS)

```bash
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
cd app
npx expo run:ios --configuration Release --device            # add --device to target a connected iPhone
```

> **Voice needs a physical iPhone.** The iOS Simulator does not capture the Mac microphone
> here, so STT won't work on the Simulator — the dark UI, navigation, and typed CRUD do.

---

## Environment variables (`backend/.env`)

| Var | Purpose |
|---|---|
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | LiveKit server (must match `livekit.yaml`) |
| `DEEPGRAM_API_KEY` | Deepgram STT + Aura TTS |
| `GROQ_API_KEY` | Groq LLM (`llama-3.3-70b-versatile`) |
| `NOTES_DB_PATH` | path to the encrypted SQLite DB (default `notes.db`) |
| `SQLCIPHER_KEY` | SQLCipher passphrase (generate: `openssl rand -hex 32`) |
| `DATABASE_URL` | PostgreSQL holding auth data in its own `user_schema` schema |
| `AUTH_JWT_SECRET` | secret used to sign session JWTs (generate: `openssl rand -hex 32`) |
| `AUTH_TOKEN_TTL_HOURS` | login session lifetime in hours (default `720` = 30 days) |
| `ADMIN_SECRET` | shared secret for the no-UI `POST /users` endpoint (`X-Admin-Token`) |

Secrets are read from `.env` only; `.env`, `*.db`, and `livekit.yaml` are gitignored. The
notes store stays encrypted at rest; only auth credentials (a bcrypt hash, never plaintext)
live in Postgres.

---

## Authentication

The app is gated by a login (email = username). Accounts live in PostgreSQL, isolated in a
dedicated **`user_schema`** schema (`user_schema.accounts`) — separate from the app's normal
`writer_app` data — while notes stay in the encrypted SQLCipher store. Sessions are
stateless **JWTs**; the app stores the token in the device keychain (`expo-secure-store`) so
you stay signed in across restarts, and sends it as `Authorization: Bearer …` when minting
LiveKit tokens.

There is **no sign-up UI** (personal use). Create accounts out-of-band against the admin-only
endpoint:

```bash
# Sign in -> JWT
curl -X POST $URL/login -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…"}'

# Create a user (admin only)
curl -X POST $URL/users -H "X-Admin-Token: $ADMIN_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…"}'
```

Backend pieces: `auth.py` (bcrypt + JWT + FastAPI guards), `user_store.py` (Postgres
`user_schema` store), and the gated `POST /token` in `token_server.py`.

---

## Testing

With the stack up (`docker compose up`):

```bash
cd backend
.venv/bin/python test_v3_flow.py      # e2e: docs/entry CRUD + context isolation
.venv/bin/python test_notes_edit.py   # legacy data-channel round-trip

# store-only (offline):
SQLCIPHER_KEY=testkey NOTES_DB_PATH=/tmp/t.db .venv/bin/python test_docs_store.py
SQLCIPHER_KEY=testkey NOTES_DB_PATH=/tmp/t2.db .venv/bin/python test_store.py

# auth (offline JWT + hashing; DB checks run only if DATABASE_URL is set):
AUTH_JWT_SECRET=testsecret .venv/bin/python test_auth.py
```

App typecheck:

```bash
cd app && npx tsc --noEmit
```

---

## Troubleshooting

- **Agent didn't get dispatched on the first test after a restart** — the worker prewarms a
  few processes; wait for `process initialized` in `docker compose logs agent` before testing.
- **iOS can't reach the backend** — the iPhone must be on the same Wi-Fi as the Mac, and the
  three files above must use the Mac's current LAN IP.
- **Code signing fails** (free Personal Team certs expire ~weekly) — re-select your team in
  Xcode → target → Signing & Capabilities, then re-run.
- **AppleDouble build failures** — see the macOS gotcha above.

---

## Deployment

The whole backend can't run on a single **Cloud Run** service: Cloud Run only serves
HTTP/WS/gRPC over one TCP port, while the LiveKit SFU needs **UDP** for WebRTC media. To run
in the cloud, host LiveKit where UDP is available (a VM, or LiveKit Cloud) and the token
server + agent alongside it.

See **[DEPLOY.md](DEPLOY.md)** for a step-by-step, lowest-cost **GCP VM** deployment:
Cloud-Console provisioning (VM + firewall, opening the UDP media port), then a fully
gcloud-driven deploy (`deploy/deploy.sh`) that builds the image **locally**, pushes it to
**Artifact Registry**, and has the VM pull + run it — **no SSH, no GitHub, no building on
the VM** — plus the iOS ATS caveat for bare-IP backends.

To put the iOS app on **another iPhone** (TestFlight, or sideloading an `.ipa` with the free
Personal Team), see **[DISTRIBUTE.md](DISTRIBUTE.md)**.
