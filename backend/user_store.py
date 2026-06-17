"""PostgreSQL-backed user store for DropNote authentication.

Auth data lives in its OWN Postgres schema — ``user_schema`` — keeping it cleanly
separated from the app's normal data (which lives in the ``writer_app`` schema). The
encrypted SQLCipher notes store (``store.py``) is unrelated and untouched: only user
credentials go to Postgres.

Schema:
    user_schema.accounts(email PK, password_hash, created_at)

The connection string comes from the ``DATABASE_URL`` environment variable; nothing is
hardcoded. Passwords are never stored in the clear — only a bcrypt hash (see ``auth.py``).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg

from auth import hash_password, verify_password

SCHEMA = "user_schema"
TABLE = f"{SCHEMA}.accounts"


@dataclass
class Account:
    email: str
    created_at: str  # ISO timestamp, for display/debug only


class UserStore:
    """Thin Postgres-backed credential store, scoped entirely to ``user_schema``.

    A fresh connection is opened per operation (psycopg manages a short-lived
    connection cleanly), mirroring the simple per-op pattern used by ``store.py``.
    """

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("DATABASE_URL is empty — cannot open the user store.")
        self._dsn = dsn
        self._init_schema()

    @classmethod
    def from_env(cls) -> "UserStore":
        return cls(dsn=os.getenv("DATABASE_URL", ""))

    @contextmanager
    def _conn(self):
        con = psycopg.connect(self._dsn)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init_schema(self) -> None:
        with self._conn() as con:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            con.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    email         TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )

    # ---- credential ops ---------------------------------------------------

    @staticmethod
    def _norm(email: str) -> str:
        return (email or "").strip().lower()

    def create_user(self, email: str, password: str) -> Account:
        """Create a new account. Raises ValueError if the email already exists."""
        email = self._norm(email)
        if not email or "@" not in email:
            raise ValueError("A valid email is required.")
        if not password:
            raise ValueError("A password is required.")
        pw_hash = hash_password(password)
        with self._conn() as con:
            row = con.execute(
                f"SELECT 1 FROM {TABLE} WHERE email = %s", (email,)
            ).fetchone()
            if row:
                raise ValueError(f"An account already exists for {email}.")
            con.execute(
                f"INSERT INTO {TABLE} (email, password_hash) VALUES (%s, %s)",
                (email, pw_hash),
            )
            created = con.execute(
                f"SELECT created_at FROM {TABLE} WHERE email = %s", (email,)
            ).fetchone()
        return Account(email=email, created_at=str(created[0]) if created else "")

    def verify_user(self, email: str, password: str) -> bool:
        """True iff the email exists and the password matches its stored hash."""
        email = self._norm(email)
        with self._conn() as con:
            row = con.execute(
                f"SELECT password_hash FROM {TABLE} WHERE email = %s", (email,)
            ).fetchone()
        if not row:
            return False
        return verify_password(password, row[0])

    def get_user(self, email: str) -> Account | None:
        email = self._norm(email)
        with self._conn() as con:
            row = con.execute(
                f"SELECT email, created_at FROM {TABLE} WHERE email = %s", (email,)
            ).fetchone()
        if not row:
            return None
        return Account(email=row[0], created_at=str(row[1]))
