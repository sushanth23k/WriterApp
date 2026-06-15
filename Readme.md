# WriterApp

Voice Memory Assistant — a hands-free voice assistant.

- **Backend** (`backend/`): self-hosted LiveKit SFU (Docker) + FastAPI token server + LiveKit Agents worker, with an encrypted SQLite store.
- **Frontend** (`app/`): Expo React Native iOS app.

## Prerequisites

- Docker (for the LiveKit server)
- Node ≥ 20.19.4 — use the keg-only Node 22 on PATH for all Expo commands:
  ```bash
  export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
  ```
- Xcode + iOS Simulator (for `expo run:ios`)
- `backend/.env` filled in (copy from `backend/.env.example`)

> The LAN IP `192.168.1.104` is hardcoded in `backend/livekit.yaml`, `backend/.env`, and `app/src/config.ts`. If your Mac's IP changes (find it with `ipconfig getifaddr en0`), update all three.

## Start the backend

The backend is three processes. Run each in its own terminal from the `backend/` directory.

```bash
cd backend
```

1. **LiveKit server (Docker):**
   ```bash
   docker compose up
   ```
   Serves WebSocket signaling on `:7880`, WebRTC TCP on `:7881`, and media on `:7882/udp`.

2. **Token server (FastAPI, port 8080):**
   ```bash
   .venv/bin/python token_server.py
   ```

3. **Agent worker (LiveKit Agents):**
   ```bash
   .venv/bin/python agent.py dev
   ```

> Use the venv interpreter (`.venv/bin/python`) — the project deps (fastapi, livekit) live only in `backend/.venv`, so the bare system/pyenv `python` fails with `ModuleNotFoundError`.

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
