#!/usr/bin/env bash
# One-shot download of every local-inference model into ./models/.
#
# These weights power the on-device audio for the "hybrid" engine (MLX Whisper STT +
# Kokoro-82M TTS) used by the LiveKit agent worker when a session is started with
# engine=hybrid. The LLM is NOT local — hybrid runs it on Groq. Nothing here is
# downloaded at runtime; the plugins load only from the dirs created below. Run this
# ONCE on the Mac that will host the worker:
#
#     bash scripts/download_models.sh
#
# Platform notes:
#   * STT weights are picked by OS: macOS -> MLX (q4) ; Linux -> faster-whisper.
#     Run this ON each machine that hosts the worker (your Mac AND the GCP VM).
#   * Kokoro phonemization needs espeak-ng (macOS: `brew install espeak-ng`; the Linux
#     Docker image installs it via apt).
#   * On-disk footprint is ~0.4-0.55 GB; both models resident is ~1.3-1.7 GB of RAM.

set -euo pipefail

# Resolve project root from this script's location so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MODELS_DIR="${PROJECT_ROOT}/models"

# Resolve the HF download CLI. huggingface_hub >= 1.0 renamed the command to `hf`
# (the old `huggingface-cli` is deprecated and no longer works); prefer the venv's
# binary, then fall back to whatever's on PATH.
VENV_BIN="${PROJECT_ROOT}/backend/.venv/bin"
if [ -x "${VENV_BIN}/hf" ]; then HF_CLI="${VENV_BIN}/hf"
elif [ -x "${VENV_BIN}/huggingface-cli" ]; then HF_CLI="${VENV_BIN}/huggingface-cli"
elif command -v hf >/dev/null 2>&1; then HF_CLI="hf"
else HF_CLI="huggingface-cli"; fi

echo "→ models dir: ${MODELS_DIR}"
mkdir -p "${MODELS_DIR}"

dl () {  # dl <repo_id> <dest_subdir>
  local repo="$1" dest="${MODELS_DIR}/$2"
  echo ""
  echo "════════════════════════════════════════════════════════════"
  echo "↓ ${repo}"
  echo "  → ${dest}"
  echo "════════════════════════════════════════════════════════════"
  "${HF_CLI}" download "${repo}" --local-dir "${dest}"
}

# 1) STT — Whisper small.en. The runtime backend differs by platform (MLX is
#    Apple-Silicon-only), so download the matching weights:
#      * macOS  -> mlx-community/whisper-small.en-mlx-q4   (mlx_whisper)
#      * Linux  -> Systran/faster-whisper-small.en         (faster-whisper / CTranslate2)
#    Override the auto-detect with: STT_BACKEND=mlx|faster bash scripts/download_models.sh
STT_BACKEND="${STT_BACKEND:-}"
if [ -z "${STT_BACKEND}" ]; then
  [ "$(uname -s)" = "Darwin" ] && STT_BACKEND="mlx" || STT_BACKEND="faster"
fi
if [ "${STT_BACKEND}" = "mlx" ]; then
  dl "mlx-community/whisper-small.en-mlx-q4" "whisper-small-en"
  WHISPER_DIR="whisper-small-en"
else
  dl "Systran/faster-whisper-small.en" "faster-whisper-small-en"
  WHISPER_DIR="faster-whisper-small-en"
fi

# 2) TTS — Kokoro-82M (cross-platform; PyTorch). The kokoro package loads config.json +
#    kokoro-v1_0.pth and the per-voice tensors under voices/ from this dir (explicit
#    paths, so no network is touched at runtime).
dl "hexgrad/Kokoro-82M" "kokoro-82m"

# NOTE: the LLM is NOT downloaded — the "hybrid" engine runs the LLM on Groq (cloud).
# Only on-device STT + TTS run locally.

echo ""
echo "── per-model size (STT backend: ${STT_BACKEND}) ───────────"
du -sh "${MODELS_DIR}/${WHISPER_DIR}" \
       "${MODELS_DIR}/kokoro-82m"

echo ""
echo "✅ All models downloaded to ./models/"
