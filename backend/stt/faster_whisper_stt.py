# RAM usage note: ~0.6 GB resident (Whisper small.en, CTranslate2 int8, CPU).
"""Linux/CPU Whisper STT as a LiveKit ``stt.STT`` plugin (drop-in for ``deepgram.STT``).

This is the cross-platform sibling of the MLX plugin: MLX is Apple-Silicon-only, so on a
Linux GCP VM (in Docker) the on-device STT runs through ``faster-whisper`` (CTranslate2)
instead. Same Whisper small.en model, no API calls. Non-streaming, so the AgentSession
wraps it with the Silero VAD (configured in agent.py).

The model loads once per process (lazy singleton) and only from ../../models/ — set
``FW_COMPUTE_TYPE`` (default ``int8``) and ``FW_DEVICE`` (default ``cpu``) to tune.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    stt,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer

sys.path.append(str(Path(__file__).resolve().parent.parent))
from model_paths import model_path  # noqa: E402

logger = logging.getLogger("local-stt")

_WHISPER_RATE = 16000  # Whisper expects 16 kHz mono float32.

_MODELS: dict[str, object] = {}  # path -> WhisperModel singleton (shared across jobs)


class FasterWhisperSTT(stt.STT):
    """Whisper small.en via faster-whisper (CTranslate2), as a non-streaming plugin."""

    def __init__(self, *, model_path_: str | None = None) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False)
        )
        self._model_path = model_path_ or model_path(
            "FW_WHISPER_MODEL", "faster-whisper-small-en"
        )
        self._device = os.getenv("FW_DEVICE", "cpu")
        self._compute_type = os.getenv("FW_COMPUTE_TYPE", "int8")

    def _ensure_loaded(self):
        m = _MODELS.get(self._model_path)
        if m is not None:
            return m
        from faster_whisper import WhisperModel

        t0 = time.perf_counter()
        m = WhisperModel(self._model_path, device=self._device, compute_type=self._compute_type)
        _MODELS[self._model_path] = m
        logger.info(
            "faster-whisper small.en loaded from %s (%s/%s) in %.0f ms",
            self._model_path, self._device, self._compute_type,
            (time.perf_counter() - t0) * 1000,
        )
        return m

    @property
    def models_loaded(self) -> bool:
        return self._model_path in _MODELS

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        text = await asyncio.to_thread(self._transcribe_sync, buffer)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="en", text=text)],
        )

    def _transcribe_sync(self, buffer: AudioBuffer) -> str:
        model = self._ensure_loaded()
        frame = rtc.combine_audio_frames(buffer)
        samples = _to_mono_16k_float32(frame)
        if samples.size == 0 or float(np.abs(samples).max()) < 1e-4:
            return ""  # silence / empty audio -> empty transcript

        t0 = time.perf_counter()
        segments, _ = model.transcribe(samples, language="en", beam_size=1)
        text = "".join(seg.text for seg in segments).strip()
        logger.info(
            "stt %.0f ms  (%.1fs audio) -> %r",
            (time.perf_counter() - t0) * 1000, samples.size / _WHISPER_RATE, text,
        )
        return text


def _to_mono_16k_float32(frame: rtc.AudioFrame) -> np.ndarray:
    """int16 PCM (any rate/channels) -> mono 16 kHz float32 in [-1, 1]."""
    data = np.frombuffer(frame.data, dtype=np.int16)
    if frame.num_channels > 1:
        data = data.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)
    if frame.sample_rate != _WHISPER_RATE:
        resampler = rtc.AudioResampler(frame.sample_rate, _WHISPER_RATE, num_channels=1)
        mono = rtc.AudioFrame(
            data=data.tobytes(), sample_rate=frame.sample_rate,
            num_channels=1, samples_per_channel=data.shape[0],
        )
        out = bytearray()
        for f in resampler.push(mono):
            out += f.data
        for f in resampler.flush():
            out += f.data
        data = np.frombuffer(bytes(out), dtype=np.int16)
    return data.astype(np.float32) / 32768.0
