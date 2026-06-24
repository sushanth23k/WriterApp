# On-device audio (Hybrid engine) — MLX

The app's **Voice Engine** toggle (Cloud / Hybrid) selects which STT/LLM/TTS stack the
agent worker runs:

| Engine | STT | LLM | TTS | Audio leaves device? |
|--------|-----|-----|-----|----------------------|
| Cloud  | Deepgram | Groq llama-3.3-70b | Deepgram | yes |
| Hybrid | MLX Whisper | Groq llama-3.3-70b | Kokoro | no (audio); LLM text does |

`Hybrid` runs **STT + TTS on-device**. The STT backend is platform-specific
(`stt/__init__.py` auto-selects it):

- **macOS (this doc)** → MLX (`mlx_whisper`). MLX has no Linux build, so on a Mac the
  agent must run **natively** (not in Docker). LiveKit + the token server stay in Docker.
- **Linux / GCP VM** → faster-whisper (CTranslate2), which DOES run in Docker. See
  **[../DEPLOY_LOCAL_GCP.md](../DEPLOY_LOCAL_GCP.md)** for the VM deployment.

TTS (Kokoro) is cross-platform. The LLM always runs on Groq (there is no on-device LLM).

## One-time setup (on the Mac)

```bash
# 1. system dep for Kokoro phonemization
brew install espeak-ng

# 2. on-device audio deps into the backend venv (macOS / MLX)
cd backend
./.venv/bin/python -m pip install -r requirements-local-mac.txt

# 3. download the model weights (~0.55 GB) into ../models/
bash ../scripts/download_models.sh

# 4. verify the audio path loads + runs (TTS -> STT -> TTS)
./.venv/bin/python scripts/smoke_local.py
```

`models/` layout after step 3:

```
models/
├── whisper-small-en/   (189 MB)  weights.npz + config.json
└── kokoro-82m/         (358 MB)  kokoro-v1_0.pth + config.json + voices/af_heart.pt
```

## Running it

```bash
cd backend
./run_agent_local.sh
```

This starts LiveKit + the token server in Docker, **stops the Docker `agent`** (so two
workers don't both grab jobs), and runs the agent on the host venv connected to
`ws://127.0.0.1:7880`. Then pick **Hybrid** in the app. `Cloud` keeps working through
the same native worker.

To go back to a pure cloud deployment, stop this process and `docker compose up -d agent`.

### Alternative: run Hybrid in Docker locally (faster-whisper, like the VM)

Plain `docker compose up` builds the **cloud-only** image, so picking **Hybrid** there
crashes with `ModuleNotFoundError: No module named 'kokoro'`. To run Hybrid in Docker on
your Mac — using **faster-whisper** in a Linux container, exactly like the GCP VM — use
the local override, which builds `Dockerfile.local` and mounts `../models`:

```bash
bash ../scripts/download_models.sh   # once: fetches faster-whisper-small.en + kokoro
cd backend
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build
```

This is the recommended way to validate the on-device pipeline before deploying to GCP.
(Native `run_agent_local.sh` above is the MLX path, which is faster on Apple Silicon but
can't run in Docker.)

## Notes / gotchas

- **Hybrid needs ~1.7 GB** of resident models (Whisper ~1 GB + Kokoro ~0.7 GB) on top
  of macOS. Comfortable on 8 GB.
- Tool-calling (the app's note navigation/saving) runs on Groq's 70B in both engines,
  so it stays reliable — that's the whole reason there's no on-device LLM.
- **First call is slow** — models load lazily on first use (singletons), so the first
  utterance after the worker starts pays the load cost. Subsequent turns are warm.
- Models are loaded ONLY from `../models/` (voice tensors by explicit path), so the
  worker does no network I/O for STT/TTS.
```
