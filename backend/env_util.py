"""Robust environment-variable reading for the DropNote backend.

Why this exists: locally we load secrets through ``python-dotenv``, which is lenient —
it tolerates ``KEY = value`` (spaces around ``=``) and strips surrounding quotes. In
deployment the same values arrive via Docker Compose's ``env_file:`` injection, whose
parser is much stricter, so a stray quote or trailing space silently produced a broken
(or missing) value and crashed the container. Reading every var through ``get()`` here
makes the app tolerant of both worlds: it always trims whitespace and one layer of
matching surrounding quotes, so a value like ``"postgresql://…"`` or `` postgres://… ``
is read identically no matter how it was supplied.
"""

from __future__ import annotations

import os


def clean(value: str | None) -> str:
    """Trim whitespace and one layer of matching surrounding quotes.

    ``'  "x"  '`` -> ``'x'``;  ``"'x'"`` -> ``'x'``;  ``None`` -> ``''``.
    """
    if value is None:
        return ""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v


def get(name: str, default: str = "", *, required: bool = False) -> str:
    """Read an env var, cleaned. Raise a clear error if ``required`` and empty/unset."""
    value = clean(os.getenv(name))
    if not value:
        if required:
            raise RuntimeError(
                f"Required environment variable {name} is missing or empty."
            )
        return default
    return value


def require(*names: str) -> None:
    """Validate that every named var is present and non-empty, else raise once.

    Lists ALL missing names in a single message so a misconfigured deploy is obvious
    from one line of logs instead of failing one variable at a time.
    """
    missing = [n for n in names if not clean(os.getenv(n))]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them in backend/.env (and they are shipped to the VM at deploy time)."
        )
