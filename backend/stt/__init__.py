"""On-device STT plugin selection.

Two backends run the SAME Whisper small.en model, picked by platform because MLX is
Apple-Silicon-only:

  * macOS (Apple Silicon)  -> MLX  (mlx_whisper)        — fast on the Neural Engine
  * Linux (e.g. GCP VM)    -> faster-whisper (CTranslate2, CPU/int8) — runs in Docker

Override with env STT_BACKEND = "mlx" | "faster". Imports are deferred so importing
this package never pulls a backend that isn't installed on the current platform.
"""

from __future__ import annotations

import os
import platform


def make_local_stt():
    """Return the on-device STT plugin appropriate for this platform."""
    backend = os.getenv("STT_BACKEND", "").strip().lower()
    if not backend:
        backend = "mlx" if platform.system() == "Darwin" else "faster"

    if backend == "mlx":
        from .mlx_whisper_stt import MLXWhisperSTT

        return MLXWhisperSTT()
    from .faster_whisper_stt import FasterWhisperSTT

    return FasterWhisperSTT()


__all__ = ["make_local_stt"]
