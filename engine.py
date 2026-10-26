"""Match Filmix/CloudDrive renames to qBittorrent and update folder plus file names."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import db

DL_STATES = {
    "downloading",
    "metaDL",
    "forcedDL",
    "stalledDL",
    "queuedDL",
    "pausedDL",
    "stoppedDL",
    "checkingDL",
    "allocating",
    "moving",
}
USER_PAUSED = {"pausedUP", "pausedDL", "stoppedUP", "stoppedDL"}
SKIP_PARTS = (".!qb", ".parts", ".temp")


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def norm_path(value: str) -> str:
    value = nfc(value.replace("\\", "/")).strip()
    if value != "/":
        value = value.rstrip("/")
    absolute = value.startswith("/")
    parts: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return ("/" if absolute else "") + "/".join(parts)


def under(parent: str, child: str) -> bool:
    parent, child = norm_path(parent), norm_path(child)
    return child == parent or child.startswith(parent + "/")


def apply_chain(path: str, events: list[dict]) -> str:
    current = norm_path(path)
    for event in events:
        src, dst = event["src"], event["dst"]
        if event["is_dir"]:
            if current == src or current.startswith(src + "/"):
                current = dst + current[len(src) :]
        elif current == src:
            current = dst
    return norm_path(current)


def chain_variants(path: str, events: list[dict]) -> list[str]:
    orders = [
        events,
        list(reversed(events)),
        sorted(events, key=lambda event: event["is_dir"]),
        sorted(events, key=lambda event: not event["is_dir"]),
    ]
    seen: list[str] = []
    for order in orders:
        got = apply_chain(path, order)
        if got not in seen:
            seen.append(got)
    return seen


def choose_new_host(host_file: str, events: list[dict]) -> str | None:
    variants = [item for item in chain_variants(host_file, events) if item != host_file]
    if not variants:
        return host_file
    existing = list(dict.fromkeys(item for item in variants if os.path.lexists(item)))
    if len(existing) > 1:
        return None
    if len(existing) == 1:
        return existing[0]
    return variants[0]


def _map_prefix(path: str, maps: list[dict], src_key: str, dst_key: str) -> str | None:
    path = norm_path(path)
    ordered = sorted(maps, key=lambda item: len(norm_path(item.get(src_key) or "")), reverse=True)
    for item in ordered:
        src = norm_path(item.get(src_key) or "")
        dst = norm_path(item.get(dst_key) or "")
        if not src or not dst:
            continue
        if path == src or path.startswith(src + "/"):
            return dst + path[len(src) :]
    return None


def cd_to_local(path: str, maps: list[dict]) -> str | None:
    return _map_prefix(path, maps, "cd_prefix", "local_prefix")


def qbit_to_local(path: str, maps: list[dict]) -> str:
    mapped = _map_prefix(path, maps, "qbit_prefix", "local_prefix")
    return mapped if mapped is not None else norm_path(path)


def _login_result(code: int, body: bytes) -> None:
    text = body.decode("utf-8", "replace").strip()
    if text == "Fails.":
        raise RuntimeError("登入失敗，帳號或密碼不正確")
    if code == 403:
        raise RuntimeError("登入被拒絕（HTTP 403）。qBittorrent 可能暫時封鎖呢個 IP")
    # 5.1 及以前：HTTP 200，內容 Ok.
    # 5.2 起：成功改為 HTTP 204，內容係空的。VueTorrent 都係用呢套 API。
    if code in (200, 204) and text in ("", "Ok."):
        return
    raise RuntimeError(f"登入失敗（HTTP {code}）")


class Qbit:
    def __init__(self, settings: dict, timeout: int = 8):
        qb = settings["qb"]
        scheme = "https" if qb.get("https") else "http"
        self.base = f"{scheme}://{qb['host']}:{int(qb['port'])}".rstrip("/")
        self.user = qb.get("username") or ""
        self.password = qb.get("password") or ""
        self.timeout = timeout
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _req(self, method: str, path: str, form: dict | None = None, query: dict | None = None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Referer": self.base + "/", "Origin": self.base}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def login(self) -> None:
        code, body = self._req(
            "POST",
            "/api/v2/auth/login",
            {"username": self.user, "password": self.password},
        )
        _login_result(code, body)

    def call(self, method: str, path: str, form: dict | None = None, query: dict | None = None):
        code, body = self._req(method, path, form, query)
        if code == 403:
            self.login()
            code, body = self._req(method, path, form, query)
        if code >= 400:
            detail = body.decode("utf-8", "replace")[:240]
            raise RuntimeError(f"qBittorrent {path} HTTP {code} {detail}")
        if not body:
            return None
        text = body.decode("utf-8", "replace")
        if text[:1] in "[{":
            return json.loads(text)
        return text

    def version(self) -> str:
        self.login()
        text = self.call("GET", "/api/v2/app/version")
        return str(text or "").strip()

    def torrents(self) -> list[dict]:
        return self.call("GET", "/api/v2/torrents/info") or []

    def files(self, infohash: str) -> list[dict]:
        return self.call("GET", "/api/v2/torrents/files", query={"hash": infohash}) or []

    def torrent(self, infohash: str) -> dict | None:
        rows = self.call("GET", "/api/v2/torrents/info", query={"hashes": infohash}) or []
        return rows[0] if rows else None

    def stop(self, infohash: str) -> None:
        try:
            self.call("POST", "/api/v2/torrents/stop", {"hashes": infohash})
        except RuntimeError:
            self.call("POST", "/api/v2/torrents/pause", {"hashes": infohash})

    def start(self, infohash: str) -> None:
        try:
            self.call("POST", "/api/v2/torrents/start", {"hashes": infohash})
        except RuntimeError:
            self.call("POST", "/api/v2/torrents/resume", {"hashes": infohash})

    def rename_file(self, infohash: str, old: str, new: str) -> None:
        self.call(
            "POST",
            "/api/v2/torrents/renameFile",
            {"hash": infohash, "oldPath": old, "newPath": new},
        )

    def rename_folder(self, infohash: str, old: str, new: str) -> None:
        self.call(
            "POST",
            "/api/v2/torrents/renameFolder",
            {"hash": infohash, "oldPath": old, "newPath": new},
        )

    def rename_torrent(self, infohash: str, name: str) -> None:
        self.call("POST", "/api/v2/torrents/rename", {"hash": infohash, "name": name})

    def recheck(self, infohash: str) -> None:
        self.call("POST", "/api/v2/torrents/recheck", {"hashes": infohash})


def probe_qb(settings: dict) -> dict:
    host = (settings.get("qb") or {}).get("host") or ""
    if not host:
        return {"ok": False, "detail": "未填 qBittorrent IP"}
    try:
        client = Qbit(settings, timeout=5)
        version = client.version()
        count = len(client.torrents())
        return {"ok": True, "detail": f"{version or '已連線'}，{count} 個種子"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc)}


def probe_cd(settings: dict) -> dict:
    cd = settings.get("clouddrive") or {}
    host = (cd.get("host") or "").strip()
    port = int(cd.get("port") or 19798)
    if not host:
        return {"ok": False, "detail": "未填 CloudDrive IP"}
    try:
        with socket.create_connection((host, port), 4):
            pass
    except OSError as exc:
        return {"ok": False, "detail": f"連不到 {host}:{port}（{exc}）"}
    scheme = "https" if cd.get("https") else "http"
    try:
        req = urllib.request.Request(f"{scheme}://{host}:{port}/", method="GET")
        with urllib.request.urlopen(req, timeout=4) as resp:
            return {"ok": True, "detail": f"{host}:{port} 有回應（HTTP {resp.status}）"}
    except urllib.error.HTTPError as exc:
        return {"ok": True, "detail": f"{host}:{port} 有回應（HTTP {exc.code}）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "detail": f"{host}:{port} 埠已開，HTTP 未確認（{exc}）"}


def probe_media(settings: dict) -> dict:
    missing = []
    seen = []
    for item in settings.get("maps") or []:
        local = norm_path(item.get("local_prefix") or "")
        if not local or local in seen:
            continue
        seen.append(local)
        if not os.path.isdir(local):
            missing.append(local)
    if not seen:
        return {"ok": False, "detail": "未設定容器內媒體路徑"}
    if missing:
        return {"ok": False, "detail": "容器內看不到：" + "、".join(missing)}
    return {"ok": True, "detail": "、".join(seen)}


def _interesting(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    if base.startswith("."):
        return False
    return not any(token in base for token in SKIP_PARTS)


def _folder_only(plans: list[dict], all_rels: list[str]) -> tuple[str, str] | None:
    if not plans:
        return None
    parents_old = set()
    parents_new = set()
    for plan in plans:
        if "/" not in plan["old_rel"] or "/" not in plan["new_rel"]:
            return None
        old_parent, old_name = plan["old_rel"].rsplit("/", 1)
        new_parent, new_name = plan["new_rel"].rsplit("/", 1)
        if old_name != new_name:
            return None
        parents_old.add(old_parent)
        parents_new.add(new_parent)
    if len(parents_old) != 1 or len(parents_new) != 1:
        return None
    old_parent = next(iter(parents_old))
    new_parent = next(iter(parents_new))
    if old_parent == new_parent:
        return None
    under_old = [rel for rel in all_rels if rel == old_parent or rel.startswith(old_parent + "/")]
    if set(under_old) != {plan["old_rel"] for plan in plans}:
        return None
    return old_parent, new_parent


class Engine:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.suppress: dict[tuple[str, str], float] = {}
        self.last_report = "待命"
        self.pending = 0
        self._qb_cache = {"ok": None, "detail": "檢查中", "at": 0}
        self._probing = False
        threading.Thread(target=self._loop, name="seedbridge", daemon=True).start()
        threading.Thread(target=self._health, name="seedbridge-health", daemon=True).start()

    def log(self, message: str) -> None:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)

    def status_qb(self) -> dict:
        age = time.time() - float(self._qb_cache.get("at") or 0)
        if age > 30 and not self._probing:
            self._probing = True
            threading.Thread(target=self._refresh_qb, daemon=True).start()
        return {"ok": self._qb_cache.get("ok"), "detail": self._qb_cache.get("detail") or "檢查中"}

    def _refresh_qb(self) -> None:
        try:
            self._qb_cache = {**probe_qb(db.get_settings()), "at": time.time()}
        except Exception as exc:  # noqa: BLE001
            self._qb_cache = {"ok": False, "detail": str(exc), "at": time.time()}
        self._probing = False

    def _health(self) -> None:
        while True:
            time.sleep(50)
            if not self._probing:
                self._probing = True
                self._refresh_qb()

    def _suppressed(self, src: str, dst: str) -> bool:
        now = time.time()
        for key, exp in list(self.suppress.items()):
            if exp < now:
                self.suppress.pop(key, None)
        return (src, dst) in self.suppress or (dst, src) in self.suppress

    def _suppress(self, src: str, dst: str, ttl: float = 120.0) -> None:
        exp = time.time() + ttl
        self.suppress[(norm_path(src), norm_path(dst))] = exp
        self.suppress[(norm_path(dst), norm_path(src))] = exp

    def add_webhook(self, payload: dict) -> int:
        items = payload.get("data") or []
        if isinstance(items, dict):
            items = [items]
        settings = db.get_settings()
        maps = settings.get("maps") or []
        added = 0
        for item in items:
            action = str(item.get("action") or "rename").lower()
            if action and action != "rename":
                continue
            src_raw = str(item.get("source_file") or "")
            dst_raw = str(item.get("destination_file") or "")
            if not src_raw or not dst_raw:
                continue
            is_dir = str(item.get("is_dir", "")).lower() in {"1", "true", "yes"}
            local_src = cd_to_local(src_raw, maps)
            local_dst = cd_to_local(dst_raw, maps)
            db.add_activity("recv", "收到 CloudDrive 改名", "", src_raw, dst_raw)
            if local_src is None or local_dst is None:
                db.add_activity(
                    "skip",
                    "路徑前綴對不上。請用呢條 source 的開頭填 CloudDrive 路徑前綴。",
                    "",
                    src_raw,
                    dst_raw,
                )
                self.log(f"unmapped {src_raw} -> {dst_raw}")
                continue
            if local_src == local_dst:
                continue
            if not is_dir and not _interesting(local_src) and not _interesting(local_dst):
                continue
            if self._suppressed(local_src, local_dst):
                self.log(f"echo {local_src}")
                continue
            with self.lock:
                self.events.append(
                    {
                        "src": local_src,
                        "dst": local_dst,
                        "is_dir": is_dir,
                        "raw_src": src_raw,
                        "raw_dst": dst_raw,
                    }
                )
                self.pending = len(self.events)
            added += 1
            self.log(f"queued {local_src} -> {local_dst}")
        if added:
            self.wake.set()
        return added

    def _loop(self) -> None:
        while True:
            self.wake.wait()
            while True:
                self.wake.clear()
                delay = float(db.get_settings().get("debounce_sec") or 8)
                if not self.wake.wait(delay):
                    break
            with self.lock:
                batch = self.events
                self.events = []
                self.pending = 0
            if batch:
                try:
                    self._process(batch)
                except Exception as exc:  # noqa: BLE001
                    self.last_report = f"處理失敗：{exc}"
                    db.add_activity("error", self.last_report)
                    self.log(self.last_report)

    def _process(self, batch: list[dict]) -> None:
        for event in batch:
            self._suppress(event["src"], event["dst"])
        settings = db.get_settings()
        if not settings.get("enabled"):
            self.last_report = f"收到 {len(batch)} 個改名，自動對齊未開啟"
            db.add_activity("skip", self.last_report)
            self.log(self.last_report)
            return
        dry = bool(settings.get("dry_run"))
        self.log(f"batch {len(batch)} dry_run={dry}")
        client = Qbit(settings)
        client.login()
        torrents = client.torrents()
        maps = settings.get("maps") or []
        candidates = []
        for tor in torrents:
            if str(tor.get("state") or "") in DL_STATES:
                continue
            host_save = qbit_to_local(str(tor.get("save_path") or ""), maps).rstrip("/")
            if not host_save:
                continue
            if any(
                under(host_save, event["src"]) or under(host_save, event["dst"]) or under(event["src"], host_save)
                for event in batch
            ):
                candidates.append((tor, host_save))
        if not candidates:
            self.last_report = "沒有完成的種子對得上呢次改名"
            db.add_activity("skip", self.last_report, "", batch[0]["raw_src"], batch[0]["raw_dst"])
            self.log(self.last_report)
            return

        claimed: dict[str, str] = {}
        ambiguous: set[str] = set()
        plans_by_hash: dict[str, dict] = {}
        for tor, host_save in candidates:
            infohash = tor["hash"]
            files = client.files(infohash)
            plans = []
            for item in files:
                rel = str(item.get("name") or "").lstrip("/")
                if not rel:
                    continue
                host_file = norm_path(host_save + "/" + rel)
                new_host = choose_new_host(host_file, batch)
                if new_host is None:
                    db.add_activity("error", "同一個檔有多於一個可能的新路徑，已跳過", tor.get("name") or "", host_file, "")
                    continue
                if new_host == host_file:
                    continue
                if not under(host_save, new_host):
                    db.add_activity(
                        "skip",
                        "新路徑離開了 qBittorrent 的儲存目錄，沒有改動",
                        tor.get("name") or "",
                        rel,
                        new_host,
                    )
                    continue
                owner = claimed.get(host_file)
                if owner and owner != infohash:
                    ambiguous.add(host_file)
                    continue
                claimed[host_file] = infohash
                plans.append(
                    {
                        "old_rel": rel,
                        "new_rel": new_host[len(norm_path(host_save)) + 1 :],
                        "host_old": host_file,
                        "host_new": new_host,
                    }
                )
            if plans:
                plans_by_hash[infohash] = {"torrent": tor, "host_save": host_save, "plans": plans, "files": files}

        if not plans_by_hash:
            self.last_report = "路徑對得上儲存目錄，但沒有種子檔需要改名"
            db.add_activity("skip", self.last_report)
            return

        done = 0
        for infohash, bag in plans_by_hash.items():
            plans = [plan for plan in bag["plans"] if plan["host_old"] not in ambiguous]
            if not plans:
                continue
            name = bag["torrent"].get("name") or infohash[:8]
            if dry:
                for plan in plans:
                    db.add_activity("skip", "只記錄，未改 qBittorrent", name, plan["old_rel"], plan["new_rel"])
                done += len(plans)
                continue
            state = str(bag["torrent"].get("state") or "")
            try:
                self._apply(
                    client,
                    infohash,
                    name,
                    plans,
                    bag["files"],
                    bag["host_save"],
                    resume=state not in USER_PAUSED,
                )
                done += len(plans)
            except Exception as exc:  # noqa: BLE001
                db.add_activity("error", str(exc), name)
                self.log(f"{infohash[:8]} {exc}")
        self.last_report = f"{'已記錄' if dry else '已自動對齊'} {done} 個路徑"
        self.log(self.last_report)

    def _apply(
        self,
        client: Qbit,
        infohash: str,
        name: str,
        plans: list[dict],
        files: list[dict],
        host_save: str,
        resume: bool,
    ) -> None:
        olds = {plan["host_old"] for plan in plans}
        news = {plan["host_new"] for plan in plans}
        if olds & news:
            raise RuntimeError("這次改名互相對調，已停止以免覆蓋檔案")
        all_rels = [str(item.get("name") or "") for item in files]
        folder = _folder_only(plans, all_rels)
        try:
            client.stop(infohash)
        except Exception as exc:  # noqa: BLE001
            self.log(f"pause skipped: {exc}")
        try:
            if folder:
                self._rename_folder(client, infohash, name, plans, folder, host_save)
            else:
                for plan in plans:
                    self._rename_file(client, infohash, name, plan)
            self._touch_display_name(client, infohash, plans, all_rels)
            self._clear_missing_if_needed(client, infohash, name)
        finally:
            if resume:
                try:
                    client.start(infohash)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"resume failed: {exc}")

    def _rename_folder(
        self,
        client: Qbit,
        infohash: str,
        name: str,
        plans: list[dict],
        folder: tuple[str, str],
        host_save: str,
    ) -> None:
        old_rel, new_rel = folder
        host_old = norm_path(host_save + "/" + old_rel)
        host_new = norm_path(host_save + "/" + new_rel)
        self._suppress(host_old, host_new)
        moved = False
        try:
            if os.path.lexists(host_new) and not os.path.lexists(host_old):
                Path(host_old).parent.mkdir(parents=True, exist_ok=True)
                os.rename(host_new, host_old)
                moved = True
            elif not os.path.lexists(host_old):
                raise RuntimeError(f"找不到資料夾 {host_old}")
            client.rename_folder(infohash, old_rel, new_rel)
            self._wait_path(client, infohash, plans[0]["new_rel"], plans[0]["host_new"])
            db.add_activity("ok", "已自動更新資料夾，並恢復做種", name, old_rel, new_rel)
        except Exception:
            if moved and os.path.lexists(host_old) and not os.path.lexists(host_new):
                Path(host_new).parent.mkdir(parents=True, exist_ok=True)
                os.rename(host_old, host_new)
            raise

    def _rename_file(self, client: Qbit, infohash: str, name: str, plan: dict) -> None:
        host_old, host_new = plan["host_old"], plan["host_new"]
        self._suppress(host_old, host_new)
        moved = False
        try:
            if os.path.lexists(host_new) and not os.path.lexists(host_old):
                Path(host_old).parent.mkdir(parents=True, exist_ok=True)
                os.rename(host_new, host_old)
                moved = True
            elif os.path.lexists(host_old) and os.path.lexists(host_new):
                raise RuntimeError("舊路徑同新路徑都有檔，沒有覆蓋")
            elif not os.path.lexists(host_old):
                raise RuntimeError("舊路徑同新路徑都找不到檔案")
            client.rename_file(infohash, plan["old_rel"], plan["new_rel"])
            self._wait_path(client, infohash, plan["new_rel"], host_new)
            db.add_activity("ok", "已自動更新檔名，並恢復做種", name, plan["old_rel"], plan["new_rel"])
        except Exception:
            if moved and os.path.lexists(host_old) and not os.path.lexists(host_new):
                Path(host_new).parent.mkdir(parents=True, exist_ok=True)
                os.rename(host_old, host_new)
            raise

    def _wait_path(self, client: Qbit, infohash: str, new_rel: str, host_new: str) -> None:
        deadline = time.time() + 45
        while time.time() < deadline:
            names = {str(item.get("name") or "") for item in client.files(infohash)}
            if new_rel in names and os.path.lexists(host_new):
                return
            time.sleep(0.4)
        raise RuntimeError(f"qBittorrent 未完成改名：{new_rel}")

    def _touch_display_name(self, client: Qbit, infohash: str, plans: list[dict], all_rels: list[str]) -> None:
        old_roots = {plan["old_rel"].split("/", 1)[0] for plan in plans if "/" in plan["old_rel"]}
        new_roots = {plan["new_rel"].split("/", 1)[0] for plan in plans if "/" in plan["new_rel"]}
        if len(old_roots) != 1 or len(new_roots) != 1 or old_roots == new_roots:
            return
        old_root, new_root = next(iter(old_roots)), next(iter(new_roots))
        if not all_rels or not all(rel == old_root or rel.startswith(old_root + "/") for rel in all_rels):
            return
        if not all(rel in {plan["old_rel"] for plan in plans} for rel in all_rels):
            return
        try:
            client.rename_torrent(infohash, new_root)
        except Exception as exc:  # noqa: BLE001
            self.log(f"display name skipped: {exc}")

    def _clear_missing_if_needed(self, client: Qbit, infohash: str, name: str) -> None:
        time.sleep(1.5)
        tor = client.torrent(infohash)
        if not tor:
            return
        if str(tor.get("state") or "") != "missingFiles":
            return
        client.recheck(infohash)
        db.add_activity("ok", "檔案已在新路徑，已自動重新檢查以清除 missing files", name)


def self_test() -> None:
    import tempfile

    events = [
        {"src": "/media/Show.S01/ep01.mkv", "dst": "/media/Show.S01/Show - S01E01.mkv", "is_dir": False},
        {"src": "/media/Show.S01", "dst": "/media/Show (2020)/Season 1", "is_dir": True},
    ]
    assert apply_chain("/media/Show.S01/ep01.mkv", events) == "/media/Show (2020)/Season 1/Show - S01E01.mkv"
    assert (
        apply_chain("/media/Show Extra/a.mkv", [{"src": "/media/Show", "dst": "/media/Other", "is_dir": True}])
        == "/media/Show Extra/a.mkv"
    )
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "B"))
        open(os.path.join(tmp, "B", "new.mkv"), "w").close()
        ev = [
            {"src": f"{tmp}/A/old.mkv", "dst": f"{tmp}/A/new.mkv", "is_dir": False},
            {"src": f"{tmp}/A", "dst": f"{tmp}/B", "is_dir": True},
        ]
        chosen = choose_new_host(f"{tmp}/A/old.mkv", list(reversed(ev)))
        assert chosen == f"{tmp}/B/new.mkv", chosen
    plans = [
        {"old_rel": "Old/a.mkv", "new_rel": "New/a.mkv"},
        {"old_rel": "Old/b.mkv", "new_rel": "New/b.mkv"},
    ]
    assert _folder_only(plans, ["Old/a.mkv", "Old/b.mkv"]) == ("Old", "New")
    assert _folder_only([{"old_rel": "Old/a.mkv", "new_rel": "New/b.mkv"}], ["Old/a.mkv"]) is None
    print("self-test ok")


ENGINE: Engine | None = None


def start() -> Engine:
    global ENGINE
    if ENGINE is None:
        ENGINE = Engine()
    return ENGINE
