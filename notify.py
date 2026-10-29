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
    lines = ["已成功改名"]
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


def _strip_forbidden(text: str) -> str:
    clean = text or ""
    for word in FORBIDDEN:
        clean = clean.replace(word, "")
    return clean


def _as_items(items) -> list:
    if not items:
        return []
    return list(items)


def _item_label(item) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return str(item or "").strip()
    for key in ("path", "file", "name", "new", "old"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def job_text(torrent: str = "", ok=None, failed=None, detail: str = "") -> str:
    """One caption for a torrent. Lists failed paths only when there are at most 10."""
    ok_items = _as_items(ok)
    failed_items = _as_items(failed)
    n_ok = len(ok_items)
    n_fail = len(failed_items)
    labels = [label for label in (_item_label(item) for item in failed_items) if label]
    lines = ["已成功改名" if n_ok else "改名失敗"]
    if str(torrent or "").strip():
        lines.append(str(torrent).strip())
    lines.append(f"成功 {n_ok} 個，失敗 {n_fail} 個")
    clean_detail = _strip_forbidden(detail or "").strip()
    fail_lines: list[str] = []
    if n_fail > 10:
        fail_lines.append("失敗超過 10 個，請查看 SeedBridge 紀錄")
        clean_detail = ""
    elif labels:
        fail_lines.append("失敗檔案：")
        fail_lines.extend(_strip_forbidden(label).strip() for label in labels)
    if clean_detail:
        fail_lines.append(clean_detail[:400])
    fail_block = _strip_forbidden("\n".join(line for line in fail_lines if line.strip()))
    text = "\n".join(lines)
    if fail_block:
        text = text + "\n" + fail_block
    if not n_ok:
        text = _strip_forbidden(text)
    return text[:1000]


def _first_poster_dir(ok) -> str:
    for item in _as_items(ok):
        if isinstance(item, dict):
            folder = str(item.get("poster_dir") or "").strip()
            if not folder:
                new = str(item.get("new") or item.get("path") or "").strip()
                folder = os.path.dirname(new) if new else ""
        else:
            folder = os.path.dirname(str(item or "").strip())
        if folder:
            return folder
    return ""


def _poster_hint(ok) -> str:
    parts: list[str] = []
    for item in _as_items(ok):
        if isinstance(item, dict):
            parts.extend(str(item.get(key) or "") for key in ("new", "old", "path", "poster_dir"))
        else:
            parts.append(str(item or ""))
    return "\n".join(parts)


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


def _credentials(settings: dict | None) -> tuple[str, str] | None:
    settings = settings or {}
    token = str(settings.get("telegram_bot_token") or "").strip()
    chat = str(settings.get("telegram_chat_id") or "").strip()
    if not token or not chat:
        return None
    return token, chat


def _load_poster(poster_dir: str, hint: str, settings: dict, opener):
    photo = find_disk_poster(poster_dir)
    if photo:
        with open(photo, "rb") as handle:
            payload = handle.read()
        filename = os.path.basename(photo)
        mime = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
        return payload, filename, mime
    fetched = fetch_tmdb_poster(hint, tmdb_key(settings), opener)
    if not fetched:
        return None
    payload, ext = fetched
    mime = "image/png" if ext == "png" else "image/jpeg"
    return payload, "poster." + ext, mime


def _deliver(token: str, chat: str, text: str, poster, opener) -> bool:
    if poster:
        payload, filename, mime = poster
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
    try:
        with opener(request, timeout=20) as response:
            response.read()
    except Exception:
        return False
    return True


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
    creds = _credentials(settings)
    if not creds:
        return False
    token, chat = creds
    settings = settings or {}
    opener = urlopen or urllib.request.urlopen
    poster = None
    if status == "ok":
        text = success_text(torrent, old_path, new_path)
        poster = _load_poster(poster_dir, f"{old_path}\n{new_path}\n{poster_dir}", settings, opener)
    else:
        text = failure_text(torrent, old_path, new_path, detail)
    return _deliver(token, chat, text, poster, opener)


def notify_job(
    torrent: str = "",
    ok=None,
    failed=None,
    detail: str = "",
    settings: dict | None = None,
    urlopen=None,
) -> bool:
    ok_items = _as_items(ok)
    failed_items = _as_items(failed)
    detail = str(detail or "")
    if not ok_items and not failed_items and not detail.strip():
        return False
    creds = _credentials(settings)
    if not creds:
        return False
    token, chat = creds
    settings = settings or {}
    opener = urlopen or urllib.request.urlopen
    text = job_text(torrent, ok_items, failed_items, detail)
    poster = None
    if ok_items:
        poster = _load_poster(_first_poster_dir(ok_items), _poster_hint(ok_items), settings, opener)
    return _deliver(token, chat, text, poster, opener)


def _stored_settings() -> dict | None:
    try:
        import db

        settings = db.get_settings()
    except Exception:
        return None
    if not _credentials(settings):
        return None
    return settings


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
    settings = _stored_settings()
    if not settings:
        return
    threading.Thread(
        target=_safe,
        args=(status, torrent, old_path, new_path, detail, poster_dir, settings),
        name="telegram",
        daemon=True,
    ).start()


def schedule_job(torrent: str = "", ok=None, failed=None, detail: str = "") -> None:
    ok_items = _as_items(ok)
    failed_items = _as_items(failed)
    detail = str(detail or "")
    if not ok_items and not failed_items and not detail.strip():
        return
    settings = _stored_settings()
    if not settings:
        return
    threading.Thread(
        target=_safe_job,
        args=(torrent, ok_items, failed_items, detail, settings),
        name="telegram-job",
        daemon=True,
    ).start()


def _safe(status, torrent, old_path, new_path, detail, poster_dir, settings) -> None:
    try:
        notify_now(status, torrent, old_path, new_path, detail, poster_dir, settings)
    except Exception as exc:  # noqa: BLE001
        print("telegram notify failed", type(exc).__name__, flush=True)


def _safe_job(torrent, ok, failed, detail, settings) -> None:
    try:
        notify_job(torrent, ok, failed, detail, settings)
    except Exception as exc:  # noqa: BLE001
        print("telegram notify failed", type(exc).__name__, flush=True)
