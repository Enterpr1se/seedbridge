"""SQLite config, login sessions, and the rename activity log."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

DEFAULT_SETTINGS = {
    "enabled": False,
    "dry_run": True,
    "debounce_sec": 8,
    "webhook_token": "",
    "qb": {
        "host": "192.168.8.197",
        "port": 8080,
        "https": False,
        "username": "admin",
        "password": "",
    },
    "clouddrive": {"host": "", "port": 19798, "https": False},
    "maps": [{"cd_prefix": "", "local_prefix": "/media", "qbit_prefix": "/downloads"}],
}


def init(data_dir: str) -> None:
    global _conn
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path / "seedbridge.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            status TEXT NOT NULL,
            torrent TEXT NOT NULL DEFAULT '',
            old_path TEXT NOT NULL DEFAULT '',
            new_path TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT ''
        );
        """
    )
    row = conn.execute("SELECT json FROM settings WHERE id = 1").fetchone()
    if row is None:
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        settings["webhook_token"] = secrets.token_urlsafe(18)
        conn.execute("INSERT INTO settings (id, json) VALUES (1, ?)", (json.dumps(settings),))
    conn.commit()
    _conn = conn


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("database not ready")
    return _conn


def has_user() -> bool:
    with _lock:
        row = _db().execute("SELECT 1 FROM users LIMIT 1").fetchone()
    return row is not None


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return salt, digest.hex()


def create_user(username: str, password: str) -> None:
    username = username.strip()
    if len(username) < 2 or len(username) > 32:
        raise ValueError("帳號長度要 2 至 32 字")
    if len(password) < 8:
        raise ValueError("密碼至少 8 個字")
    salt, digest = _hash_password(password)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        if _db().execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise ValueError("已經有管理員")
        _db().execute(
            "INSERT INTO users (username, salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (username, salt, digest, now),
        )
        _db().commit()


def check_login(username: str, password: str) -> bool:
    with _lock:
        row = _db().execute(
            "SELECT salt, password_hash FROM users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
    if row is None:
        _hash_password(password)
        return False
    salt, digest = _hash_password(password, row["salt"])
    return hmac.compare_digest(digest, row["password_hash"])


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + 14 * 24 * 3600
    with _lock:
        _db().execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
        _db().execute(
            "INSERT INTO sessions (token, username, expires_at) VALUES (?, ?, ?)",
            (token, username.strip(), expires),
        )
        _db().commit()
    return token


def session_user(token: str | None) -> str | None:
    if not token:
        return None
    now = int(time.time())
    with _lock:
        row = _db().execute(
            "SELECT username, expires_at FROM sessions WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None or row["expires_at"] < now:
            if row is not None:
                _db().execute("DELETE FROM sessions WHERE token = ?", (token,))
                _db().commit()
            return None
    return row["username"]


def delete_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _db().execute("DELETE FROM sessions WHERE token = ?", (token,))
        _db().commit()


def get_settings() -> dict:
    with _lock:
        row = _db().execute("SELECT json FROM settings WHERE id = 1").fetchone()
    data = json.loads(row["json"]) if row else json.loads(json.dumps(DEFAULT_SETTINGS))
    for key, value in DEFAULT_SETTINGS.items():
        data.setdefault(key, value)
    return data


def save_settings(data: dict) -> dict:
    current = get_settings()
    qb = data.get("qb") or {}
    prev_qb = current.get("qb") or {}
    password = str(qb.get("password") or "")
    if not password:
        password = str(prev_qb.get("password") or "")
    maps = []
    for item in data.get("maps") or []:
        maps.append(
            {
                "cd_prefix": str(item.get("cd_prefix") or "").strip(),
                "local_prefix": str(item.get("local_prefix") or "").strip() or "/media",
                "qbit_prefix": str(item.get("qbit_prefix") or "").strip(),
            }
        )
    if not maps:
        maps = json.loads(json.dumps(DEFAULT_SETTINGS["maps"]))
    cd = data.get("clouddrive") or {}
    try:
        debounce = int(data.get("debounce_sec") or 8)
    except (TypeError, ValueError):
        debounce = 8
    debounce = min(60, max(2, debounce))
    token = str(data.get("webhook_token") or current.get("webhook_token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(18)
    stored = {
        "enabled": bool(data.get("enabled")),
        "dry_run": bool(data.get("dry_run")),
        "debounce_sec": debounce,
        "webhook_token": token,
        "qb": {
            "host": str(qb.get("host") or "").strip(),
            "port": _port(qb.get("port"), 8080),
            "https": bool(qb.get("https")),
            "username": str(qb.get("username") or "").strip(),
            "password": password,
        },
        "clouddrive": {
            "host": str(cd.get("host") or "").strip(),
            "port": _port(cd.get("port"), 19798),
            "https": bool(cd.get("https")),
        },
        "maps": maps,
    }
    with _lock:
        _db().execute("UPDATE settings SET json = ? WHERE id = 1", (json.dumps(stored),))
        _db().commit()
    return stored


def public_settings(data: dict | None = None) -> dict:
    data = json.loads(json.dumps(data or get_settings()))
    qb = data.get("qb") or {}
    qb["password_set"] = bool(qb.get("password"))
    qb["password"] = ""
    data["qb"] = qb
    return data


def add_activity(status: str, detail: str, torrent: str = "", old_path: str = "", new_path: str = "") -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        _db().execute(
            "INSERT INTO activity (ts, status, torrent, old_path, new_path, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, status, torrent[:300], old_path[:1000], new_path[:1000], detail[:1000]),
        )
        _db().execute("DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT 2000)")
        _db().commit()


def list_activity(limit: int = 200) -> list[dict]:
    limit = min(500, max(1, limit))
    with _lock:
        rows = _db().execute(
            "SELECT id, ts, status, torrent, old_path, new_path, detail FROM activity ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def activity_counts() -> dict:
    today = time.strftime("%Y-%m-%d")
    with _lock:
        rows = _db().execute(
            "SELECT status, COUNT(*) AS n FROM activity WHERE ts LIKE ? GROUP BY status",
            (today + "%",),
        ).fetchall()
    counts = {"ok": 0, "error": 0, "skip": 0, "recv": 0}
    for row in rows:
        if row["status"] in counts:
            counts[row["status"]] = row["n"]
    return counts


def _port(value, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    if port < 1 or port > 65535:
        return default
    return port
