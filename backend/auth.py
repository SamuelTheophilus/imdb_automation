from __future__ import annotations

import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from backend.db import (
    create_user,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    update_user_password,
)

# In-memory store for one-time reset codes: {code: {user_id, expires_at}}
_reset_tokens: dict[str, dict] = {}
_RESET_EXPIRY_SECONDS = 600  # 10 minutes


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


def _check_password_strength(password: str) -> str | None:
    """Return an error message if the password fails strength requirements, else None."""
    import re
    if len(password) < 8:
        return "Password must be at least 8 characters"
    if not re.search(r"[A-Z]", password):
        return "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return "Password must contain at least one lowercase letter"
    if not re.search(r"\d", password):
        return "Password must contain at least one number"
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?]", password):
        return "Password must contain at least one special character"
    return None


def signup(
    username: str, password: str, email: str = ""
) -> tuple[bool, str, str | None]:
    """Create a user and return `(success, message, token)` for the UI."""
    import re

    username = username.strip().lower()
    email = email.strip().lower()

    if len(username) < 3:
        return False, "Username must be at least 3 characters", None
    if not re.match(r"^[a-z0-9_]+$", username):
        return False, "Username may only contain letters, numbers, and underscores", None

    pw_error = _check_password_strength(password)
    if pw_error:
        return False, pw_error, None

    if email and not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        return False, "Enter a valid email address", None

    if get_user_by_username(username):
        return False, "Username already taken", None
    if email and get_user_by_email(email):
        return False, "An account with that email already exists", None

    user_id = create_user(username, hash_password(password), email or None)
    return True, "Account created", create_access_token(user_id)


def login(username: str, password: str) -> tuple[bool, str, str | None]:
    """Validate credentials and return `(success, message, token)` for the UI."""
    username = username.strip().lower()
    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        return False, "Invalid username or password", None

    return True, "Logged in", create_access_token(user["id"])


def request_password_reset(username: str) -> tuple[bool, str, str | None]:
    """Generate a one-time reset code and email it to the user's registered address.

    Returns (success, error_message_or_empty_string, email_or_none).
    The code is never logged or displayed — it travels only via email.
    """
    from backend.email_service import send_password_reset

    user = get_user_by_username(username.strip().lower())
    if not user:
        return False, "No account found with that username", None

    email = user.get("email")
    if not email:
        return False, "No email address is registered for this account", None

    # Purge expired codes before adding a new one
    now = time.time()
    for k in [k for k, v in _reset_tokens.items() if v["expires_at"] < now]:
        del _reset_tokens[k]

    code = secrets.token_hex(3).upper()  # 6-char hex e.g. "A3F7B2"
    _reset_tokens[code] = {"user_id": user["id"], "expires_at": now + _RESET_EXPIRY_SECONDS}

    try:
        send_password_reset(email, code)
    except Exception as exc:
        del _reset_tokens[code]
        print(f"[auth] Failed to send reset email to {email}: {exc}")
        return False, "Failed to send reset email. Please try again later.", None

    return True, "", email


def consume_reset_code(code: str, new_password: str) -> tuple[bool, str]:
    """Validate a reset code, update the password, and invalidate the code."""
    code = code.strip().upper()
    entry = _reset_tokens.get(code)
    if not entry or time.time() > entry["expires_at"]:
        _reset_tokens.pop(code, None)
        return False, "Invalid or expired reset code"
    if len(new_password) < 6:
        return False, "Password must be at least 6 characters"
    update_user_password(entry["user_id"], hash_password(new_password))
    del _reset_tokens[code]
    return True, "Password updated successfully"


def change_password(
    user_id: int, current_password: str, new_password: str
) -> tuple[bool, str]:
    """Change password for an already-authenticated user."""
    user = get_user_by_id(user_id)
    if not user or not verify_password(current_password, user["password_hash"]):
        return False, "Current password is incorrect"
    if len(new_password) < 6:
        return False, "New password must be at least 6 characters"
    if current_password == new_password:
        return False, "New password must differ from current password"
    update_user_password(user_id, hash_password(new_password))
    return True, "Password changed successfully"
