#!/usr/bin/env python3
"""Seedbridge WebUI and CloudDrive webhook."""

from __future__ import annotations

import json
import os
import sys
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db

DATA_DIR = os.environ.get("DATA_DIR") or str(Path(__file__).resolve().parent / "data")
db.init(DATA_DIR)

import engine  # noqa: E402

engine.start()

ROOT = Path(__file__).resolve().parent / "static"
LOGIN_FAILS: dict[str, list[float]] = {}


def _cookie_token(header: str | None) -> str | None:
    if not header:
        return None
    jar = SimpleCookie()
    jar.load(header)
    morsel = jar.get("sb_session")
    return morsel.value if morsel else None


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return handler.client_address[0]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Seedbridge"

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict, extra: dict | None = None) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 2_000_000:
            raise ValueError("內容太大")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("需要 JSON 物件")
        return data

    def _user(self) -> str | None:
        return db.session_user(_cookie_token(self.headers.get("Cookie")))

    def _require(self) -> str | None:
        user = self._user()
        if not user:
            self._json(401, {"error": "請先登入"})
            return None
        return user

    def _session_cookie(self, token: str, clear: bool = False) -> dict:
        if clear:
            value = "sb_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
        else:
            value = f"sb_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={14 * 24 * 3600}"
        return {"Set-Cookie": value}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        if path in {"/", "/index.html"}:
            self._file("index.html", "text/html; charset=utf-8")
            return
        if path == "/app.css":
            self._file("app.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._file("app.js", "text/javascript; charset=utf-8")
            return
        if path == "/favicon.svg":
            self._send(200, _FAVICON, "image/svg+xml")
            return
        routes = {
            "/api/session": self._session,
            "/api/settings": self._get_settings,
            "/api/activity": self._activity,
            "/api/overview": self._overview,
        }
        if path in routes:
            routes[path]()
            return
        self._json(404, {"error": "找不到"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/file_notify", "/api/webhook"}:
            self._webhook()
            return
        routes = {
            "/api/setup": self._setup,
            "/api/login": self._login,
            "/api/logout": self._logout,
            "/api/settings": self._save_settings,
            "/api/test/qb": self._test_qb,
            "/api/test/cd": self._test_cd,
            "/api/test/media": self._test_media,
            "/api/arm": self._arm,
        }
        if path not in routes:
            self._json(404, {"error": "找不到"})
            return
        try:
            routes[path]()
        except json.JSONDecodeError:
            self._json(400, {"error": "JSON 不正確"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    def _file(self, name: str, content_type: str) -> None:
        target = ROOT / name
        if not target.is_file():
            self._json(404, {"error": "找不到"})
            return
        self._send(200, target.read_bytes(), content_type)

    def _session(self) -> None:
        user = self._user()
        self._json(200, {"user": user, "setup": not db.has_user()})

    def _setup(self) -> None:
        if db.has_user():
            self._json(400, {"error": "已經完成初次設定"})
            return
        data = self._read_json()
        username = str(data.get("username") or "admin")
        password = str(data.get("password") or "")
        confirm = str(data.get("confirm") or "")
        if password != confirm:
            raise ValueError("兩次密碼不相同")
        db.create_user(username, password)
        token = db.create_session(username)
        self._json(200, {"user": username}, self._session_cookie(token))

    def _login(self) -> None:
        ip = _client_ip(self)
        now = time.time()
        fails = [stamp for stamp in LOGIN_FAILS.get(ip, []) if now - stamp < 600]
        if len(fails) >= 8:
            self._json(429, {"error": "試太多次，請 10 分鐘後再試"})
            return
        data = self._read_json()
        username = str(data.get("username") or "")
        password = str(data.get("password") or "")
        if not db.check_login(username, password):
            fails.append(now)
            LOGIN_FAILS[ip] = fails
            self._json(401, {"error": "帳號或密碼不正確"})
            return
        LOGIN_FAILS.pop(ip, None)
        token = db.create_session(username)
        self._json(200, {"user": username}, self._session_cookie(token))

    def _logout(self) -> None:
        db.delete_session(_cookie_token(self.headers.get("Cookie")))
        self._json(200, {"ok": True}, self._session_cookie("", clear=True))

    def _get_settings(self) -> None:
        if not self._require():
            return
        self._json(200, db.public_settings())

    def _save_settings(self) -> None:
        if not self._require():
            return
        saved = db.save_settings(self._read_json())
        engine.ENGINE._qb_cache["at"] = 0
        self._json(200, db.public_settings(saved))

    def _merged_settings(self, override: dict | None = None) -> dict:
        base = db.get_settings()
        if not override:
            return base
        data = json.loads(json.dumps(base))
        for key in ("qb", "clouddrive"):
            if key in override:
                data[key].update(override[key])
                if key == "qb" and not str(override["qb"].get("password") or ""):
                    data["qb"]["password"] = base["qb"].get("password") or ""
        if "maps" in override:
            data["maps"] = override["maps"]
        return data

    def _test_qb(self) -> None:
        if not self._require():
            return
        self._json(200, engine.probe_qb(self._merged_settings(self._read_json())))

    def _test_cd(self) -> None:
        if not self._require():
            return
        self._json(200, engine.probe_cd(self._merged_settings(self._read_json())))

    def _test_media(self) -> None:
        if not self._require():
            return
        self._json(200, engine.probe_media(self._merged_settings(self._read_json())))

    def _arm(self) -> None:
        if not self._require():
            return
        settings = db.get_settings()
        qb = engine.probe_qb(settings)
        media = engine.probe_media(settings)
        maps = settings.get("maps") or []
        prefixes = [item for item in maps if item.get("cd_prefix") and item.get("qbit_prefix") and item.get("local_prefix")]
        if not qb.get("ok"):
            self._json(400, {"error": "qBittorrent 未連上：" + qb.get("detail", "")})
            return
        if not media.get("ok"):
            self._json(400, {"error": media.get("detail") or "媒體目錄不存在"})
            return
        if not prefixes:
            self._json(400, {"error": "請先填好路徑對應"})
            return
        settings["enabled"] = True
        settings["dry_run"] = False
        db.save_settings(settings)
        db.add_activity("ok", "已開啟自動對齊。之後 Filmix 改名會自動更新 qBittorrent。")
        self._json(200, db.public_settings())

    def _activity(self) -> None:
        if not self._require():
            return
        query = parse_qs(urlparse(self.path).query)
        limit = int((query.get("limit") or ["200"])[0] or 200)
        self._json(200, {"items": db.list_activity(limit)})

    def _overview(self) -> None:
        if not self._require():
            return
        settings = db.get_settings()
        self._json(
            200,
            {
                "enabled": settings.get("enabled"),
                "dry_run": settings.get("dry_run"),
                "pending": engine.ENGINE.pending,
                "last_report": engine.ENGINE.last_report,
                "qb": engine.ENGINE.status_qb(),
                "media": engine.probe_media(settings),
                "counts": db.activity_counts(),
                "recent": db.list_activity(12),
            },
        )

    def _webhook(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        settings = db.get_settings()
        token = settings.get("webhook_token") or ""
        if token and not _webhook_ok(self, token):
            db.add_activity("error", "Webhook 被拒絕：token 不符")
            self._json(401, {"error": "token"})
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "bad json"})
            return
        if urlparse(self.path).path == "/api/webhook" and "data" not in payload and payload.get("source_file"):
            payload = {"data": [payload]}
        added = engine.ENGINE.add_webhook(payload)
        self._json(200, {"ok": True, "queued": added})

    def log_message(self, fmt: str, *args) -> None:
        engine.ENGINE.log("http " + (fmt % args))


def _webhook_ok(handler: BaseHTTPRequestHandler, token: str) -> bool:
    query = parse_qs(urlparse(handler.path).query)
    supplied = (query.get("token") or [""])[0]
    supplied = supplied or handler.headers.get("X-Webhook-Token", "")
    auth = handler.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = supplied or auth[7:].strip()
    return supplied == token


_FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="8" fill="#1e211b"/>'
    b'<path d="M8 11h16v2.2H8zm0 5.2h10V18H8zm0 5.3h13v2.2H8z" fill="#d7a15a"/>'
    b"</svg>"
)


def main() -> None:
    if "--self-test" in sys.argv:
        engine.self_test()
        return
    host = os.environ.get("LISTEN", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Seedbridge listening on {host}:{port}  data={DATA_DIR}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
