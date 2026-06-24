# RAM usage note: ~1.0 GB resident (Whisper small.en, 4-bit MLX).
"""Local Whisper STT as a LiveKit ``stt.STT`` plugin (drop-in for ``deepgram.STT``).

mlx_whisper is batch/non-streaming, so this declares ``streaming=False``; the
AgentSession wraps it with the Silero VAD (already configured in agent.py) to cut the
mic stream into utterances and call :meth:`_recognize_impl` per utterance. The model
is loaded once per process (lazy singleton) and shared across every session/job.

All inference is on-device — no network. Weights load only from ../../models/.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    stt,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from model_paths import model_path  # noqa: E402

logger = logging.getLogger("local-stt")

_WHISPER_RATE = 16000  # Whisper always expects 16 kHz mono float32.


class MLXWhisperSTT(stt.STT):
    """Whisper small.en via mlx_whisper, exposed as a non-streaming STT plugin."""

    def __init__(self, *, model_path_: str | None = None) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False)
        )
        self._model_path = model_path_ or model_path("WHISPER_MODEL", "whisper-small-en")
        self._loaded = False

    # Lazy singleton: warm the model once, on the first transcription, so importing
    # this module (e.g. for a cloud-engine session) never touches MLX or the weights.
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        import mlx_whisper  # noqa: F401  (import = a cheap no-op; weights load on first call)

        t0 = time.perf_counter()
        # mlx_whisper has no separate load(); a tiny silent clip primes the weights so
        # the first real utterance isn't penalised with the load cost.
        mlx_whisper.transcribe(
            np.zeros(_WHISPER_RATE // 10, dtype=np.float32),
            path_or_hf_repo=self._model_path,
        )
        self._loaded = True
        logger.info(
            "Whisper small.en loaded from %s in %.0f ms",
            self._model_path,
            (time.perf_counter() - t0) * 1000,
        )

    @property
    def models_loaded(self) -> bool:
        return self._loaded

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        # Heavy work (numpy + MLX) is blocking; keep the event loop free.
        text = await asyncio.to_thread(self._transcribe_sync, buffer)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="en", text=text)],
        )

    def _transcribe_sync(self, buffer: AudioBuffer) -> str:
        self._ensure_loaded()
        import mlx_whisper

        frame = rtc.combine_audio_frames(buffer)
        samples = self._to_mono_16k_float32(frame)
        if samples.size == 0 or float(np.abs(samples).max()) < 1e-4:
            return ""  # silence / empty audio -> empty transcript

        t0 = time.perf_counter()
        result = mlx_whisper.transcribe(
            samples, path_or_hf_repo=self._model_path, language="en", fp16=True
        )
        text = (result.get("text") or "").strip()
        logger.info(
            "stt %.0f ms  (%.1fs audio) -> %r",
            (time.perf_counter() - t0) * 1000,
            samples.size / _WHISPER_RATE,
            text,
        )
        return text

    @staticmethod
    def _to_mono_16k_float32(frame: rtc.AudioFrame) -> np.ndarray:
        """int16 PCM (any rate/channels) -> mono 16 kHz float32 in [-1, 1]."""
        data = np.frombuffer(frame.data, dtype=np.int16)
        if frame.num_channels > 1:
            data = data.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)
        if frame.sample_rate != _WHISPER_RATE:
            resampler = rtc.AudioResampler(frame.sample_rate, _WHISPER_RATE, num_channels=1)
            mono = rtc.AudioFrame(
                data=data.tobytes(),
                sample_rate=frame.sample_rate,
                num_channels=1,
                samples_per_channel=data.shape[0],
            )
            out = bytearray()
            for f in resampler.push(mono):
                out += f.data
            for f in resampler.flush():
                out += f.data
            data = np.frombuffer(bytes(out), dtype=np.int16)
        return data.astype(np.float32) / 32768.0
