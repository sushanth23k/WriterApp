"""Auth smoke tests for DropNote.

Two tiers:
  - OFFLINE (always runs): bcrypt hashing/verify + JWT mint/decode/expiry. No DB.
  - DB (runs only if DATABASE_URL is set): exercises the Postgres user store against a
    throwaway schema so it never touches your real `user_schema`/`writer_app` data.

Run:
    AUTH_JWT_SECRET=testsecret .venv/bin/python test_auth.py
    DATABASE_URL=postgresql://... AUTH_JWT_SECRET=testsecret .venv/bin/python test_auth.py
"""

from __future__ import annotations

import os
import time

# Ensure a signing secret exists for the offline JWT checks.
os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-do-not-use-in-prod")

import jwt  # noqa: E402

import auth  # noqa: E402


def test_password_hashing() -> None:
    h = auth.hash_password("hunter2")
    assert h != "hunter2", "password must not be stored in clear"
    assert auth.verify_password("hunter2", h) is True
    assert auth.verify_password("wrong", h) is False
    assert auth.verify_password("hunter2", "not-a-hash") is False
    print("ok  password hashing/verify")


def test_jwt_roundtrip() -> None:
    tok = auth.create_access_token("alice@example.com")
    assert auth.decode_token(tok) == "alice@example.com"
    print("ok  jwt mint/decode")


def test_jwt_expiry() -> None:
    # Force a token that is already expired.
    now = int(time.time())
    payload = {"sub": "bob@example.com", "iat": now - 100, "exp": now - 10}
    tok = jwt.encode(payload, auth._jwt_secret(), algorithm=auth.JWT_ALGORITHM)
    try:
        auth.decode_token(tok)
    except jwt.ExpiredSignatureError:
        print("ok  jwt expiry rejected")
        return
    raise AssertionError("expired token should have raised ExpiredSignatureError")


def test_user_store_if_db() -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("skip user-store DB tests (set DATABASE_URL to run them)")
        return

    # Run against an isolated throwaway schema so we never touch real data.
    import psycopg

    import user_store as us

    test_schema = "user_schema_test"
    us.SCHEMA = test_schema
    us.TABLE = f"{test_schema}.accounts"

    store = us.UserStore(dsn)
    try:
        email = f"test-{int(time.time())}@example.com"
        store.create_user(email, "s3cret!")
        assert store.verify_user(email, "s3cret!") is True
        assert store.verify_user(email, "nope") is False
        assert store.get_user(email) is not None

        duplicated = False
        try:
            store.create_user(email, "again")
        except ValueError:
            duplicated = True
        assert duplicated, "duplicate email must be rejected"
        print("ok  user store create/verify/duplicate (DB)")
    finally:
        with psycopg.connect(dsn) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {test_schema} CASCADE")
            con.commit()


if __name__ == "__main__":
    test_password_hashing()
    test_jwt_roundtrip()
    test_jwt_expiry()
    test_user_store_if_db()
    print("\nAll auth tests passed.")
