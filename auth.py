"""
iStealClips — Persistent Signed HMAC Cookie Authentication
"""

import json
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any
import config

USERS_FILE = config.DATA_DIR / "users.json"
SECRET_KEY = "istealclips_permanent_secret_key_2026_x99"


def _load_users() -> Dict[str, Any]:
    if not USERS_FILE.exists():
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_users(users: Dict[str, Any]):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)


def hash_password(password: str) -> str:
    return hashlib.sha256(f"istealclips_salt_{password}".encode()).hexdigest()


def register_user(username: str, password: str) -> bool:
    users = _load_users()
    uname = username.strip().lower()
    if not uname or not password:
        return False
    if uname in users:
        return False
    users[uname] = {
        "username": uname,
        "password": hash_password(password)
    }
    _save_users(users)
    return True


def verify_user(username: str, password: str) -> bool:
    users = _load_users()
    uname = username.strip().lower()
    if not uname or not password:
        return False
    
    # If no users exist in system yet, auto-create the account!
    if len(users) == 0:
        return register_user(username, password)

    if uname not in users:
        # Auto register new user on login attempt
        return register_user(username, password)

    return users[uname]["password"] == hash_password(password)


def create_session(username: str) -> str:
    """Generates a self-verifying signed session token."""
    uname = username.strip().lower()
    sig = hashlib.sha256(f"{SECRET_KEY}:{uname}".encode()).hexdigest()[:16]
    return f"{uname}:{sig}"


def get_session_user(token: Optional[str]) -> Optional[str]:
    """Validates signed session token without needing a session database."""
    if not token or ":" not in token:
        return None
    try:
        parts = token.split(":", 1)
        uname = parts[0].strip().lower()
        expected_sig = hashlib.sha256(f"{SECRET_KEY}:{uname}".encode()).hexdigest()[:16]
        if parts[1] == expected_sig:
            return uname
    except Exception:
        pass
    return None


def delete_session(token: str):
    pass
