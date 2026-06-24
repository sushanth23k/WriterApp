"""Resolve on-disk locations of the local-inference model weights.

Every local plugin (STT/LLM/TTS) loads ONLY from these directories — nothing is
downloaded at runtime (see scripts/download_models.sh). Paths are resolved relative
to the project root so the worker can be started from any working directory, with an
optional ``MODELS_DIR`` env override (absolute, or relative to the project root).
"""

from __future__ import annotations

import os
from pathlib import Path

# backend/model_paths.py -> backend/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def models_dir() -> Path:
    # MODELS_DIR may be absolute, or relative to the project root (default "models",
    # i.e. <project root>/models where download_models.sh writes the weights).
    raw = os.getenv("MODELS_DIR", "models")
    p = Path(raw)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def model_path(env_name: str, default_subdir: str) -> str:
    """Absolute path to one model dir, e.g. model_path("WHISPER_MODEL", "whisper-small-en")."""
    sub = os.getenv(env_name, default_subdir)
    return str(models_dir() / sub)
