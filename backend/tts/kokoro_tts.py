# RAM usage note: ~0.7 GB resident (Kokoro-82M + voice tensors).
"""Local Kokoro-82M TTS as a LiveKit ``tts.TTS`` plugin (drop-in for ``deepgram.TTS``).

Kokoro renders a whole utterance at once, so this is a non-streaming (ChunkedStream)
TTS emitting 24 kHz mono 16-bit PCM — the AudioEmitter resamples to whatever the
LiveKit session needs. The pipeline + weights load once per process and only from
../../models/ (voice tensors are passed as explicit local paths, so nothing is fetched
at runtime).

System dep: Kokoro phonemization needs espeak-ng (`brew install espeak-ng`).
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    tts,
    utils,
)

sys.path.append(str(Path(__file__).resolve().parent.parent))
from model_paths import model_path  # noqa: E402

logger = logging.getLogger("local-tts")

_RATE = 24000  # Kokoro always renders at 24 kHz mono.
_LANG = "a"    # American English.

_PIPELINE = {}  # path -> KPipeline singleton


def _load_pipeline(path: str):
    if path in _PIPELINE:
        return _PIPELINE[path]
    from kokoro import KModel, KPipeline

    t0 = time.perf_counter()
    cfg = os.path.join(path, "config.json")
    weights = next(iter(glob.glob(os.path.join(path, "*.pth"))), None)
    # Prefer fully-local load (explicit config + weights); fall back to repo download
    # only if the expected files aren't present on disk.
    if os.path.exists(cfg) and weights:
        kmodel = KModel(repo_id="hexgrad/Kokoro-82M", config=cfg, model=weights)
    else:
        logger.warning("kokoro: local weights not found in %s; using hub default", path)
        kmodel = KModel(repo_id="hexgrad/Kokoro-82M")
    pipeline = KPipeline(lang_code=_LANG, repo_id="hexgrad/Kokoro-82M", model=kmodel)
    logger.info("Kokoro-82M loaded from %s in %.0f ms", path, (time.perf_counter() - t0) * 1000)
    _PIPELINE[path] = pipeline
    return pipeline


class KokoroTTS(tts.TTS):
    def __init__(self, *, model_path_: str | None = None, voice: str | None = None) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=_RATE,
            num_channels=1,
        )
        self._model_path = model_path_ or model_path("TTS_MODEL", "kokoro-82m")
        self._voice = voice or os.getenv("TTS_VOICE", "af_heart")
        self._loaded = False

    @property
    def models_loaded(self) -> bool:
        return self._loaded

    def _voice_arg(self) -> str:
        # An existing .pt path keeps voice loading offline; otherwise the bare name.
        p = os.path.join(self._model_path, "voices", f"{self._voice}.pt")
        return p if os.path.exists(p) else self._voice

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "KokoroChunkedStream":
        return KokoroChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    # Blocking render (runs in a thread): text -> int16 PCM bytes @ 24 kHz mono.
    def _render(self, text: str) -> bytes:
        pipeline = _load_pipeline(self._model_path)
        self._loaded = True
        t0 = time.perf_counter()
        chunks: list[np.ndarray] = []
        for result in pipeline(text, voice=self._voice_arg()):
            audio = result[2] if isinstance(result, tuple) else result.audio
            if audio is None:
                continue
            arr = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            chunks.append(arr.astype(np.float32).reshape(-1))
        if not chunks:
            return b""
        wave = np.concatenate(chunks)
        pcm = (np.clip(wave, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        logger.info(
            "tts %.0f ms  (%d chars -> %.1fs audio)",
            (time.perf_counter() - t0) * 1000, len(text), len(wave) / _RATE,
        )
        return pcm


class KokoroChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        pcm = await asyncio.to_thread(self._tts._render, self._input_text)
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=_RATE,
            num_channels=1,
            mime_type="audio/pcm",
        )
        if pcm:
            output_emitter.push(pcm)
        output_emitter.flush()
