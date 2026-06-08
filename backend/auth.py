from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from backend.db import create_user, get_user_by_id, get_user_by_username


JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24


def _jwt_secret() -> str:
    """Return the signing secret used for local session tokens.

    For a hackathon/demo app this environment fallback is acceptable, but a
    deployed app should set `IMDB_AUTH_SECRET` to a long random value.
    """
    return os.getenv("IMDB_AUTH_SECRET", "imdb-local-dev-secret")


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt before storing it."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a login password against the stored bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(user_id: int) -> str:
    """Create a short JSON Web Token that NiceGUI stores in user session storage."""
    expires_at = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": str(user_id), "exp": expires_at},
        _jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )


def verify_access_token(token: str | None) -> dict | None:
    """Decode a JWT and return the linked user, or None for invalid sessions."""
    if not token:
        return None

    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None

    try:
        return get_user_by_id(int(user_id))
    except ValueError:
        return None


def signup(username: str, password: str) -> tuple[bool, str, str | None]:
    """Create a user and return `(success, message, token)` for the UI."""
    username = username.strip().lower()
    if len(username) < 3:
        return False, "Username must be at least 3 characters", None
    if len(password) < 6:
        return False, "Password must be at least 6 characters", None
    if get_user_by_username(username):
        return False, "Username already exists", None

    user_id = create_user(username, hash_password(password))
    return True, "Account created", create_access_token(user_id)


def login(username: str, password: str) -> tuple[bool, str, str | None]:
    """Validate credentials and return `(success, message, token)` for the UI."""
    username = username.strip().lower()
    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        return False, "Invalid username or password", None

    return True, "Logged in", create_access_token(user["id"])
