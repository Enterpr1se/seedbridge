"""Telegram notices for rename success and failure. Network errors never abort a rename."""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.request

POSTER_NAMES = ("poster.jpg", "folder.jpg", "cover.jpg", "poster.png", "folder.png")
FORBIDDEN = (
    "评分",
    "評分",
    "类型",
    "類型",
    "媒体库",
    "媒體庫",
    "TMDB ID",
    "IMDB ID",
    "时间",
    "時間",
    "剧情",
    "劇情",
    "新入库",
)
_TMDB_ID = re.compile(r"\[tmdbid=(\d+)\]", re.IGNORECASE)


def success_text(torrent: str, old_path: str, new_path: str) -> str:
    lines = ["成功改名"]
    if torrent:
        lines.append(torrent)
    if old_path or new_path:
        lines.append(f"{old_path} → {new_path}")
    return "\n".join(lines)[:1000]


def failure_text(torrent: str, old_path: str, new_path: str, detail: str) -> str:
    clean = detail or ""
    for word in FORBIDDEN:
        clean = clean.replace(word, "")
    lines = ["改名失敗"]
    if torrent:
        lines.append(torrent)
    if old_path:
        lines.append(old_path)
    if new_path:
        lines.append(new_path)
    clean = clean.strip()
    if clean:
        lines.append(clean[:400])
    text = "\n".join(lines)
    for word in FORBIDDEN:
        text = text.replace(word, "")
    return text[:1000]


def find_disk_poster(folder: str) -> str | None:
    if not folder or not os.path.isdir(folder):
        return None
    found = {name.lower(): name for name in os.listdir(folder)}
    for want in POSTER_NAMES:
        actual = found.get(want)
        if not actual:
            continue
        path = os.path.join(folder, actual)
        if os.path.isfile(path):
            return path
    return None


def tmdb_key(settings: dict | None) -> str:
    stored = str((settings or {}).get("tmdb_api_key") or "").strip()
    if stored:
        return stored
    return os.environ.get("TMDB_API_KEY", "").strip()


def _is_bearer(key: str) -> bool:
    return key.startswith("eyJ") and key.count(".") == 2


def fetch_tmdb_poster(hint: str, key: str, urlopen=None) -> tuple[bytes, str] | None:
    if not key:
        return None
    match = _TMDB_ID.search(hint or "")
    if not match:
        return None
    opener = urlopen or urllib.request.urlopen
    movie_id = match.group(1)
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?language=zh-HK"
    headers = {"Accept": "application/json"}
    if _is_bearer(key):
        headers["Authorization"] = f"Bearer {key}"
    else:
        url += "&api_key=" + urllib.request.quote(key, safe="")
    try:
        request = urllib.request.Request(url, headers=headers)
        with opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return None
    poster_path = str((payload or {}).get("poster_path") or "")
    if not poster_path.startswith("/"):
        return None
    image_url = "https://image.tmdb.org/t/p/w500" + poster_path
    try:
        image_req = urllib.request.Request(image_url)
        with opener(image_req, timeout=20) as response:
            body = response.read()
    except Exception:
        return None
    if not body:
        return None
    ext = "png" if poster_path.lower().endswith(".png") else "jpg"
    return body, ext


def _multipart(fields: dict[str, str], filename: str, content: bytes, mime: str) -> tuple[str, bytes]:
    boundary = "----SeedBridgeBoundary"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        + content
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, b"".join(chunks)


def notify_now(
    status: str,
    torrent: str = "",
    old_path: str = "",
    new_path: str = "",
    detail: str = "",
    poster_dir: str = "",
    settings: dict | None = None,
    urlopen=None,
) -> bool:
    if status not in {"ok", "error"}:
        return False
    settings = settings or {}
    token = str(settings.get("telegram_bot_token") or "").strip()
    chat = str(settings.get("telegram_chat_id") or "").strip()
    if not token or not chat:
        return False
    opener = urlopen or urllib.request.urlopen
    if status == "ok":
        text = success_text(torrent, old_path, new_path)
        photo = find_disk_poster(poster_dir)
        payload = None
        filename = "poster.jpg"
        mime = "image/jpeg"
        if photo:
            with open(photo, "rb") as handle:
                payload = handle.read()
            filename = os.path.basename(photo)
            mime = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
        else:
            fetched = fetch_tmdb_poster(f"{old_path}\n{new_path}\n{poster_dir}", tmdb_key(settings), opener)
            if fetched:
                payload, ext = fetched
                filename = "poster." + ext
                mime = "image/png" if ext == "png" else "image/jpeg"
        if payload:
            boundary, body = _multipart({"chat_id": chat, "caption": text}, filename, payload, mime)
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        else:
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps({"chat_id": chat, "text": text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
    else:
        text = failure_text(torrent, old_path, new_path, detail)
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    try:
        with opener(request, timeout=20) as response:
            response.read()
    except Exception:
        return False
    return True


def schedule(
    status: str,
    torrent: str = "",
    old_path: str = "",
    new_path: str = "",
    detail: str = "",
    poster_dir: str = "",
) -> None:
    if status not in {"ok", "error"}:
        return
    try:
        import db

        settings = db.get_settings()
    except Exception:
        return
    if not str(settings.get("telegram_bot_token") or "").strip():
        return
    if not str(settings.get("telegram_chat_id") or "").strip():
        return
    threading.Thread(
        target=_safe,
        args=(status, torrent, old_path, new_path, detail, poster_dir, settings),
        name="telegram",
        daemon=True,
    ).start()


def _safe(status, torrent, old_path, new_path, detail, poster_dir, settings) -> None:
    try:
        notify_now(status, torrent, old_path, new_path, detail, poster_dir, settings)
    except Exception as exc:  # noqa: BLE001
        print("telegram notify failed", type(exc).__name__, flush=True)
