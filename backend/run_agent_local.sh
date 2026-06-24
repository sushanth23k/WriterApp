#!/usr/bin/env bash
# Run the LiveKit Agents worker NATIVELY on macOS so the "hybrid" engine's on-device
# audio (MLX Whisper STT + Kokoro TTS) actually loads — MLX has no Linux build, so it
# cannot run in the Docker `agent` container. (The hybrid LLM runs on Groq.)
#
# Topology while this runs:
#   • LiveKit SFU + token server  -> stay in Docker  (docker compose)
#   • agent worker                -> this process, on the host venv
#
# The Docker `agent` service is stopped first so the two workers don't both grab jobs
# (the Docker one is cloud-only and can't do MLX). Cloud-only deployments keep using
# the Docker agent and ignore this script.
#
# Usage:   cd backend && ./run_agent_local.sh

set -euo pipefail
cd "$(dirname "$0")"  # backend/

VENV_PY="./.venv/bin/python"
MODELS_DIR="../models"

# --- preflight ---------------------------------------------------------------------
[ -x "$VENV_PY" ] || { echo "✗ backend/.venv not found — create it and pip install -r requirements.txt"; exit 1; }
for m in whisper-small-en kokoro-82m; do
  [ -d "${MODELS_DIR}/${m}" ] || { echo "✗ missing model: ${MODELS_DIR}/${m} — run: bash ../scripts/download_models.sh"; exit 1; }
done
command -v espeak-ng >/dev/null 2>&1 || echo "⚠ espeak-ng not on PATH — Kokoro TTS needs it (brew install espeak-ng)"
"$VENV_PY" -c "import mlx_whisper, kokoro" 2>/dev/null || { echo "✗ MLX/Kokoro not installed in venv — pip install mlx mlx-whisper kokoro soundfile"; exit 1; }

# --- bring up the rest of the stack in Docker, minus the agent ---------------------
if command -v docker >/dev/null 2>&1; then
  echo "→ starting LiveKit + token-server in Docker, stopping the Docker agent…"
  docker compose up -d livekit token-server
  docker compose stop agent 2>/dev/null || true
else
  echo "⚠ docker not found — make sure LiveKit + the token server are running some other way"
fi

# --- run the agent on the host -----------------------------------------------------
# Override LIVEKIT_URL to the host-published port (the .env value targets the internal
# compose network / the phone's LAN). load_dotenv(override=False) won't clobber this.
export LIVEKIT_URL="${LIVEKIT_URL:-ws://127.0.0.1:7880}"
echo "→ agent connecting to LiveKit at ${LIVEKIT_URL}"
echo "  (pick Hybrid in the app for on-device STT/TTS; Cloud still works too)"
exec "$VENV_PY" agent.py start
