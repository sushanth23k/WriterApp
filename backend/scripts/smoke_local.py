#!/usr/bin/env python
"""Standalone smoke test for the local (MLX) audio stack — no LiveKit needed.

The "hybrid" engine runs STT + TTS on-device (the LLM stays on Groq), so this
exercises the REAL plugin code paths for that audio loop:

    Kokoro TTS  ──render──▶  MLX Whisper STT  ──transcribe──▶  (compare text)

It reports load times + per-stage latency. Run from the host (NOT Docker — MLX is
macOS-only):

    ./.venv/bin/python scripts/smoke_local.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow imports of the backend packages (stt/tts/model_paths) from scripts/.
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import soundfile as sf  # noqa: E402
from livekit import rtc  # noqa: E402

from stt.mlx_whisper_stt import MLXWhisperSTT  # noqa: E402
from tts.kokoro_tts import KokoroTTS  # noqa: E402

OUT = BACKEND / "scripts" / "out"
OUT.mkdir(parents=True, exist_ok=True)

USER_UTTERANCE = "Add milk and eggs to my grocery list, then tell me what is on it."
REPLY = "Done. Your grocery list now has milk and eggs."


def _section(t: str) -> None:
    print(f"\n{'─' * 60}\n{t}\n{'─' * 60}")


def _pcm_to_f32(pcm: bytes):
    import numpy as np

    return np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0


def main() -> int:
    timings: dict[str, float] = {}

    # ---- TTS: render the user's utterance to audio (stands in for a mic) ----------
    _section("1) Kokoro TTS  — render test utterance")
    tts = KokoroTTS()
    t0 = time.perf_counter()
    pcm = tts._render(USER_UTTERANCE)  # 24 kHz mono int16
    timings["tts_render_ms"] = (time.perf_counter() - t0) * 1000
    if not pcm:
        print("✗ TTS produced no audio"); return 1
    sf.write(OUT / "1_user.wav", _pcm_to_f32(pcm), 24000)
    print(f"✓ {len(pcm)//2} samples ({len(pcm)/2/24000:.1f}s) -> {OUT/'1_user.wav'}")

    # ---- STT: transcribe that audio back to text ----------------------------------
    _section("2) MLX Whisper STT — transcribe it back")
    stt = MLXWhisperSTT()
    frame = rtc.AudioFrame(
        data=pcm, sample_rate=24000, num_channels=1, samples_per_channel=len(pcm) // 2
    )
    t0 = time.perf_counter()
    transcript = stt._transcribe_sync([frame])
    timings["stt_ms"] = (time.perf_counter() - t0) * 1000
    print(f"✓ transcript: {transcript!r}")

    # ---- TTS: render a canned assistant reply (what Groq would return in hybrid) ---
    _section("3) Kokoro TTS — render a reply")
    t0 = time.perf_counter()
    rpcm = tts._render(REPLY)
    timings["tts_reply_ms"] = (time.perf_counter() - t0) * 1000
    sf.write(OUT / "3_reply.wav", _pcm_to_f32(rpcm), 24000)
    print(f"✓ -> {OUT/'3_reply.wav'}")

    # ---- summary -------------------------------------------------------------------
    _section("SUMMARY (per-stage latency, ms)")
    for k, v in timings.items():
        print(f"  {k:16s} {v:8.0f}")
    print("\nNote: first calls include lazy model load; warm turns are much faster.")
    print("✅ Local STT + TTS (the hybrid audio path) loaded and ran end-to-end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
