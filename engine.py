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


def _split_parent(rel: str) -> tuple[str, str]:
    rel = norm_path(rel).lstrip("/")
    if "/" not in rel:
        return "", rel
    parent, name = rel.rsplit("/", 1)
    return parent, name


def intermediate_rel(old_rel: str, new_rel: str) -> str | None:
    """Filename already updated, still inside the old folder. None when only one side changes."""
    old_parent, _old_name = _split_parent(old_rel)
    new_parent, _new_name = _split_parent(new_rel)
    if not old_parent or not new_parent or old_parent == new_parent:
        return None
    if not old_rel.startswith(old_parent + "/") or not new_rel.startswith(new_parent + "/"):
        return None
    old_rest = old_rel[len(old_parent) + 1 :]
    new_rest = new_rel[len(new_parent) + 1 :]
    if old_rest == new_rest:
        return None
    return old_parent + "/" + new_rest


def stage_ops(plans: list[dict]) -> list[dict]:
    """Split a folder+file rename into qBittorrent steps.

    When both parent and filename change, issue renameFile old→mid (new name
    still under the old folder), then renameFile mid→final (parent only).
    This qBittorrent's renameFolder can return 200, create an empty dest
    directory, and leave torrent file paths unchanged.
    Same-filename trees (parent only) still use one renameFolder, with a
    per-file renameFile fallback in execute_plans.
    """
    usable = [plan for plan in plans if norm_path(plan["old_rel"]) != norm_path(plan["new_rel"])]
    if not usable:
        return []
    parsed = []
    parents_old: set[str] = set()
    parents_new: set[str] = set()
    for plan in usable:
        old_parent, _old_name = _split_parent(plan["old_rel"])
        new_parent, _new_name = _split_parent(plan["new_rel"])
        parents_old.add(old_parent)
        parents_new.add(new_parent)
        parsed.append((plan, old_parent, new_parent))
    one_tree = len(parents_old) == 1 and len(parents_new) == 1
    old_parent = next(iter(parents_old)) if one_tree else ""
    new_parent = next(iter(parents_new)) if one_tree else ""
    folder_changes = bool(one_tree and old_parent and new_parent and old_parent != new_parent)
    rooted = folder_changes and all(
        plan["old_rel"].startswith(old_parent + "/") and plan["new_rel"].startswith(new_parent + "/")
        for plan, _old_parent, _new_parent in parsed
    )
    if rooted:
        ops = []
        rest_changed = False
        for plan, _old_parent, _new_parent in parsed:
            old_rest = plan["old_rel"][len(old_parent) + 1 :]
            new_rest = plan["new_rel"][len(new_parent) + 1 :]
            if old_rest != new_rest:
                rest_changed = True
                ops.append(
                    {
                        "kind": "file",
                        "old": old_parent + "/" + old_rest,
                        "new": old_parent + "/" + new_rest,
                    }
                )
        if rest_changed:
            for plan, _old_parent, _new_parent in parsed:
                new_rest = plan["new_rel"][len(new_parent) + 1 :]
                ops.append(
                    {
                        "kind": "file",
                        "old": old_parent + "/" + new_rest,
                        "new": new_parent + "/" + new_rest,
                    }
                )
            return ops
        ops.append({"kind": "folder", "old": old_parent, "new": new_parent})
        return ops
    return [{"kind": "file", "old": plan["old_rel"], "new": plan["new_rel"]} for plan in usable]


def remaining_ops(plans: list[dict], names: set[str] | list[str]) -> list[dict]:
    have = {norm_path(name) for name in names}
    ops = []
    for op in stage_ops(plans):
        if op["kind"] == "file":
            if norm_path(op["new"]) in have:
                continue
            if _file_op_past(op, plans, have):
                continue
        if op["kind"] == "folder" and all(norm_path(plan["new_rel"]) in have for plan in plans):
            continue
        ops.append(op)
    return ops


def _file_op_past(op: dict, plans: list[dict], have: set[str]) -> bool:
    """True when qBit already has a later name for the plan this file op belongs to."""
    target = norm_path(op["new"])
    for plan in plans:
        if norm_path(plan["new_rel"]) not in have:
            continue
        mid = intermediate_rel(plan["old_rel"], plan["new_rel"])
        if mid and target == norm_path(mid):
            return True
    return False


class RenameFailed(RuntimeError):
    def __init__(self, message: str, retry: str = "") -> None:
        super().__init__(message)
        self.retry = retry


def _names_of(rows: list[dict]) -> set[str]:
    return {norm_path(str(item.get("name") or "")) for item in rows if item.get("name")}


def _disk_for(host_save: str, rel: str) -> str:
    return norm_path(host_save.rstrip("/") + "/" + rel.lstrip("/"))


def _candidate_disks(plan: dict, host_save: str) -> list[str]:
    rels = [plan["old_rel"], plan["new_rel"]]
    mid = intermediate_rel(plan["old_rel"], plan["new_rel"])
    if mid:
        rels.insert(1, mid)
    paths = [_disk_for(host_save, rel) for rel in rels]
    paths.append(plan["host_old"])
    paths.append(plan["host_new"])
    unique: list[str] = []
    for path in paths:
        if path and path not in unique:
            unique.append(path)
    return unique


def restore_filmix(plans: list[dict], host_save: str) -> None:
    """Put bytes back on the Filmix path. Never deletes and never overwrites."""
    for plan in plans:
        dest = plan["host_new"]
        if os.path.lexists(dest):
            continue
        for src in _candidate_disks(plan, host_save):
            if src == dest or not os.path.lexists(src):
                continue
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dest)
            break


def _qbit_rel(plan: dict, names: set[str]) -> str:
    if norm_path(plan["new_rel"]) in names:
        return plan["new_rel"]
    mid = intermediate_rel(plan["old_rel"], plan["new_rel"])
    if mid and norm_path(mid) in names:
        return mid
    return plan["old_rel"]


def plan_has_bytes(plan: dict, host_save: str) -> bool:
    """True when this torrent file has bytes at the old, mid, or Filmix path."""
    paths = [plan.get("host_old") or "", plan.get("host_new") or ""]
    mid = intermediate_rel(plan.get("old_rel") or "", plan.get("new_rel") or "")
    if mid:
        paths.append(_disk_for(host_save, mid))
    return any(path and os.path.lexists(path) for path in paths)


def align_disk_to_qbit(plans: list[dict], names: set[str], host_save: str) -> None:
    for plan in plans:
        if not plan_has_bytes(plan, host_save):
            continue
        # Filmix/CloudDrive may already hold the final bytes. Leave them there;
        # waiting after renameFile accepts host_new in place of a missing mid.
        if os.path.lexists(plan["host_new"]):
            continue
        expected = _disk_for(host_save, _qbit_rel(plan, names))
        if os.path.lexists(expected):
            continue
        source = ""
        if os.path.lexists(plan["host_old"]):
            source = plan["host_old"]
        if not source:
            continue
        if source == expected:
            continue
        if os.path.lexists(expected):
            raise RenameFailed("舊路徑同新路徑都有檔，沒有覆蓋")
        Path(expected).parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, expected)


def _file_disk_alts(op: dict, plans: list[dict], host_save: str) -> list[str]:
    """Stage target, or the matching plan's Filmix final path for a mid/final rename."""
    alts = [_disk_for(host_save, op["new"])]
    op_new = norm_path(op["new"])
    for plan in plans:
        mid = intermediate_rel(plan["old_rel"], plan["new_rel"])
        if op_new != norm_path(plan["new_rel"]) and (not mid or op_new != norm_path(mid)):
            continue
        host_new = plan.get("host_new") or ""
        if host_new and host_new not in alts:
            alts.append(host_new)
    return alts


def _strictly_under(parent: str, child: str) -> bool:
    """True when child is inside parent. The save path itself is not inside."""
    parent, child = norm_path(parent), norm_path(child)
    if not parent or not child:
        return False
    return child.startswith(parent + "/")


def _unique_paths(paths: list[str]) -> list[str]:
    unique: list[str] = []
    for path in paths:
        if not path:
            continue
        path = norm_path(path)
        if path not in unique:
            unique.append(path)
    return unique


def _matching_plans(op: dict, plans: list[dict]) -> list[dict]:
    source = norm_path(op["old"])
    target = norm_path(op["new"])
    matched = []
    for plan in plans:
        rels = {norm_path(plan["old_rel"]), norm_path(plan["new_rel"])}
        mid = intermediate_rel(plan["old_rel"], plan["new_rel"])
        if mid:
            rels.add(norm_path(mid))
        if source in rels or target in rels:
            matched.append(plan)
    return matched


def _final_covers_mid(op: dict, plans: list[dict]) -> bool:
    """Filmix already left this file at host_new, so a mid stage must not pull it back."""
    if op.get("kind") != "file":
        return False
    target = norm_path(op["new"])
    for plan in plans:
        mid = intermediate_rel(plan["old_rel"], plan["new_rel"])
        if not mid or norm_path(mid) != target:
            continue
        host_new = plan.get("host_new") or ""
        if host_new and os.path.lexists(host_new):
            return True
    return False


def _file_sources(op: dict, plans: list[dict], host_save: str) -> list[str]:
    """Candidate bytes for a file stage: old path, mid, host_old, then host_new if this is the final name."""
    target = norm_path(op["new"])
    primary = [_disk_for(host_save, op["old"])]
    mids: list[str] = []
    olds: list[str] = []
    finals: list[str] = []
    for plan in _matching_plans(op, plans):
        mid = intermediate_rel(plan["old_rel"], plan["new_rel"])
        if mid:
            mids.append(_disk_for(host_save, mid))
        olds.append(_disk_for(host_save, plan["old_rel"]))
        if plan.get("host_old"):
            olds.append(plan["host_old"])
        host_new = plan.get("host_new") or ""
        if host_new and target == norm_path(plan["new_rel"]) and (not mid or target != norm_path(mid)):
            finals.append(host_new)
    return _unique_paths(primary + mids + olds + finals)


def _plan_file_sources(plan: dict, host_save: str, current_rel: str) -> list[str]:
    sources = []
    if current_rel:
        sources.append(_disk_for(host_save, current_rel))
    sources.append(_disk_for(host_save, plan["old_rel"]))
    mid = intermediate_rel(plan["old_rel"], plan["new_rel"])
    if mid:
        sources.append(_disk_for(host_save, mid))
    if plan.get("host_old"):
        sources.append(plan["host_old"])
    if plan.get("host_new"):
        sources.append(plan["host_new"])
    return _unique_paths(sources)


def _ensure_disk_at(host_save: str, src_candidates: list[str], dest_rel: str) -> bool:
    """Put bytes at dest_rel inside host_save. Return True when a rename happened.

    qBittorrent 5.3 rename APIs are no-ops unless this path already exists.
    Sources outside host_save are ignored. host_save itself is never renamed.
    """
    host = norm_path(host_save)
    dest = _disk_for(host, dest_rel)
    if not _strictly_under(host, dest):
        raise RenameFailed(f"目標路徑離開儲存目錄：{dest_rel}")
    if os.path.lexists(dest):
        return False
    for src in src_candidates:
        if not src:
            continue
        src = norm_path(src)
        if src == dest or not _strictly_under(host, src):
            continue
        if not os.path.lexists(src):
            continue
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        os.rename(src, dest)
        return True
    return False


def _ensure_op_disk(op: dict, plans: list[dict], host_save: str) -> bool:
    if op["kind"] == "folder":
        if not any(plan_has_bytes(plan, host_save) for plan in plans):
            return False
        return _ensure_disk_at(host_save, [_disk_for(host_save, op["old"])], op["new"])
    matched = [plan for plan in _matching_plans(op, plans) if plan_has_bytes(plan, host_save)]
    if not matched:
        return False
    if _final_covers_mid(op, matched):
        return False
    return _ensure_disk_at(host_save, _file_sources(op, matched, host_save), op["new"])


def _client_quiet(client, name: str, infohash: str) -> None:
    method = getattr(client, name, None)
    if method is None:
        return
    try:
        method(infohash)
    except Exception:  # noqa: BLE001
        return


def _disk_group_ready(group: list[str]) -> bool:
    paths = [path for path in group if path]
    if not paths:
        return True
    return any(os.path.lexists(path) for path in paths)


def _byte_plans(plans: list[dict], host_save: str) -> list[dict]:
    return [plan for plan in plans if plan_has_bytes(plan, host_save)]


def _names_ready(plans: list[dict], names: set[str]) -> bool:
    return all(norm_path(plan["new_rel"]) in names for plan in plans)


def _downloaded_ready(plans: list[dict], names: set[str], host_save: str) -> bool:
    downloaded = _byte_plans(plans, host_save)
    return all(
        norm_path(plan["new_rel"]) in names and os.path.lexists(plan["host_new"]) for plan in downloaded
    )


def _disk_groups_for(plans: list[dict], host_save: str) -> list[list[str]]:
    groups = [[plan["host_new"]] for plan in plans if plan_has_bytes(plan, host_save)]
    return groups or [[]]


def _file_disk_groups(op: dict, plans: list[dict], host_save: str) -> list[list[str]]:
    matched = [plan for plan in _matching_plans(op, plans) if plan_has_bytes(plan, host_save)]
    if not matched:
        return [[]]
    return [_file_disk_alts(op, matched, host_save)]


def _wait_ready(fetch, expect_rels: list[str], disk_groups: list[list[str]], timeout: float, pause: float, sleep, now) -> bool:
    deadline = now() + timeout
    while True:
        names = _names_of(fetch())
        rels_ok = all(norm_path(rel) in names for rel in expect_rels)
        disks_ok = all(_disk_group_ready(group) for group in disk_groups)
        if rels_ok and disks_ok:
            return True
        if now() >= deadline:
            return False
        sleep(pause)


def _retry_blob(infohash: str, name: str, host_save: str, plans: list[dict]) -> str:
    return json.dumps(
        {
            "infohash": infohash,
            "name": name,
            "host_save": host_save,
            "plans": [
                {
                    "old_rel": plan["old_rel"],
                    "new_rel": plan["new_rel"],
                    "host_old": plan["host_old"],
                    "host_new": plan["host_new"],
                }
                for plan in plans
            ],
        },
        ensure_ascii=False,
    )


def execute_plans(
    client,
    infohash: str,
    plans: list[dict],
    host_save: str,
    *,
    timeout: float = 60,
    pause: float = 1.0,
    sleep=time.sleep,
    now=time.time,
    log=None,
    resume: bool = True,
) -> None:
    """Stop, move bytes onto each stage target, then rename in qBittorrent.

    qBittorrent 5.3 can answer HTTP 200 from renameFile/renameFolder and leave
    torrent paths unchanged unless the new path is already on disk. On failure,
    bytes return to the Filmix names. resume=False leaves a user-paused torrent stopped.
    """

    def say(message: str) -> None:
        if log:
            log(message)

    def fetch() -> list[dict]:
        return client.files(infohash) or []

    def _start() -> None:
        if resume:
            _client_quiet(client, "start", infohash)

    for plan in plans:
        if not plan_has_bytes(plan, host_save):
            say(f"未下載：只更新 qB 路徑，不搬磁碟 {plan.get('old_rel') or ''}")
    if not plans:
        _start()
        return

    names = _names_of(fetch())
    if plans and all(
        norm_path(plan["new_rel"]) in names
        and (not plan_has_bytes(plan, host_save) or os.path.lexists(plan["host_new"]))
        for plan in plans
    ):
        say("qBittorrent 已是新路徑")
        _start()
        return
    try:
        _client_quiet(client, "stop", infohash)
        align_disk_to_qbit(plans, names, host_save)
        ops = remaining_ops(plans, _names_of(fetch()))
        if not ops:
            _start()
            return
        for index, op in enumerate(ops, start=1):
            say(f"階段 {index}/{len(ops)} {op['kind']} {op['old']} → {op['new']}")
            if _ensure_op_disk(op, plans, host_save):
                say(f"磁碟已搬到 {op['new']}")
            if op["kind"] == "folder":
                client.rename_folder(infohash, op["old"], op["new"])
                expect_rels = [plan["new_rel"] for plan in plans]
                disk_groups = _disk_groups_for(plans, host_save)
                label = "資料夾"
                if _wait_ready(fetch, expect_rels, disk_groups, timeout, pause, sleep, now):
                    continue
                say(f"renameFolder 未完成，改為逐檔 renameFile：{op['old']} → {op['new']}")
                names_now = _names_of(fetch())
                for plan in plans:
                    current = _qbit_rel(plan, names_now)
                    if norm_path(current) == norm_path(plan["new_rel"]):
                        continue
                    if plan_has_bytes(plan, host_save) and _ensure_disk_at(
                        host_save, _plan_file_sources(plan, host_save, current), plan["new_rel"]
                    ):
                        say(f"磁碟已搬到 {plan['new_rel']}")
                    client.rename_file(infohash, current, plan["new_rel"])
                if _wait_ready(fetch, expect_rels, disk_groups, timeout, pause, sleep, now):
                    continue
                if _byte_plans(plans, host_save) and not _downloaded_ready(plans, _names_of(fetch()), host_save):
                    raise RenameFailed(f"qBittorrent 未完成改名（{label}）：{op['new']}")
                for plan in plans:
                    if plan_has_bytes(plan, host_save):
                        continue
                    if norm_path(plan["new_rel"]) not in _names_of(fetch()):
                        say(f"未下載：qB 路徑未跟上 {plan['old_rel']}")
                continue
            client.rename_file(infohash, op["old"], op["new"])
            expect_rels = [op["new"]]
            disk_groups = _file_disk_groups(op, plans, host_save)
            label = "檔名"
            if _wait_ready(fetch, expect_rels, disk_groups, timeout, pause, sleep, now):
                continue
            if any(plan_has_bytes(plan, host_save) for plan in _matching_plans(op, plans)):
                raise RenameFailed(f"qBittorrent 未完成改名（{label}）：{op['new']}")
            say(f"未下載：qB 路徑未跟上 {op['new']}")
    except Exception as exc:
        try:
            downloaded = _byte_plans(plans, host_save)
            downloaded_ok = _downloaded_ready(plans, _names_of(fetch()), host_save)
        except Exception:
            downloaded = [plans[0]] if plans else [None]
            downloaded_ok = False
        if not downloaded or downloaded_ok:
            say("qBittorrent 已跟上最終路徑" if downloaded_ok else "未下載的檔未能全部更新 qB 路徑，已略過")
            _start()
            return
        try:
            restore_filmix(plans, host_save)
        except OSError as restore_exc:
            say(f"撥回 Filmix 路徑失敗：{restore_exc}")
        _start()
        if isinstance(exc, RenameFailed):
            raise
        raise RenameFailed(str(exc)) from exc
    _start()


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
        self._retries: set[int] = set()
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
                new_rel = new_host[len(norm_path(host_save)) + 1 :]
                draft = {
                    "old_rel": rel,
                    "new_rel": new_rel,
                    "host_old": host_file,
                    "host_new": new_host,
                }
                if not plan_has_bytes(draft, host_save):
                    db.add_activity("skip", "未下載：只更新 qB 路徑，不搬磁碟", tor.get("name") or "", rel, new_rel)
                    self.log(f"未下載：只更新 qB 路徑，不搬磁碟 {rel}")
                plans.append(draft)
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
                blob = getattr(exc, "retry", "") or _retry_blob(infohash, name, bag["host_save"], plans)
                db.add_activity("error", str(exc), name, plans[0]["old_rel"], plans[0]["new_rel"], blob)
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
        blob = _retry_blob(infohash, name, host_save, plans)
        if olds & news:
            raise RenameFailed("這次改名互相對調，已停止以免覆蓋檔案", blob)
        for plan in plans:
            self._suppress(plan["host_old"], plan["host_new"])
            mid = intermediate_rel(plan["old_rel"], plan["new_rel"])
            if mid:
                middle = _disk_for(host_save, mid)
                self._suppress(plan["host_old"], middle)
                self._suppress(middle, plan["host_new"])
        try:
            client.stop(infohash)
        except Exception as exc:  # noqa: BLE001
            self.log(f"pause skipped: {exc}")
        try:
            execute_plans(client, infohash, plans, host_save, log=self.log, resume=resume)
            for plan in plans:
                db.add_activity("ok", "已自動更新路徑，並恢復做種", name, plan["old_rel"], plan["new_rel"])
            if db.get_settings().get("rename_torrent_title"):
                all_rels = [str(item.get("name") or "") for item in files]
                self._touch_display_name(client, infohash, plans, all_rels)
        except Exception as exc:
            if not getattr(exc, "retry", ""):
                exc.retry = blob
            raise
        finally:
            if resume:
                try:
                    client.start(infohash)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"resume failed: {exc}")

    def retry(self, activity_id: int) -> None:
        row = db.get_activity(activity_id)
        if not row or row.get("status") != "error" or not row.get("retry"):
            raise ValueError("這條紀錄不能重試。更新前的失敗沒有保存路徑，要等下一次改名，或者按新的失敗列。")
        with self.lock:
            if activity_id in self._retries:
                raise ValueError("這條正在重試")
            self._retries.add(activity_id)
        threading.Thread(
            target=self._retry_worker,
            args=(activity_id, row["retry"]),
            name=f"retry-{activity_id}",
            daemon=True,
        ).start()

    def _retry_worker(self, activity_id: int, raw: str) -> None:
        try:
            payload = json.loads(raw)
            plans = payload["plans"]
            host_save = payload["host_save"]
            infohash = payload["infohash"]
            name = payload.get("name") or infohash[:8]
            for plan in plans:
                self._suppress(plan["host_old"], plan["host_new"])
            client = Qbit(db.get_settings())
            client.login()
            tor = client.torrent(infohash)
            resume = str((tor or {}).get("state") or "") not in USER_PAUSED
            db.add_activity("ok", "開始重試，唔使先改回舊名", name, plans[0]["old_rel"], plans[0]["new_rel"])
            try:
                client.stop(infohash)
            except Exception as exc:  # noqa: BLE001
                self.log(f"pause skipped: {exc}")
            try:
                execute_plans(client, infohash, plans, host_save, log=self.log, resume=resume)
                for plan in plans:
                    db.add_activity("ok", "重試成功，已更新 qBittorrent", name, plan["old_rel"], plan["new_rel"])
            except Exception as exc:  # noqa: BLE001
                blob = getattr(exc, "retry", "") or raw
                db.add_activity("error", str(exc), name, plans[0]["old_rel"], plans[0]["new_rel"], blob)
                self.log(f"retry {infohash[:8]} {exc}")
            finally:
                if resume:
                    try:
                        client.start(infohash)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"resume failed: {exc}")
        finally:
            with self.lock:
                self._retries.discard(activity_id)

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
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log(f"display name skipped: {exc}")


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
    movie = [
        {
            "old_rel": "Old Title/Old.iso",
            "new_rel": "New Title (2026) [tmdbid=1]/New.iso",
        }
    ]
    ops = stage_ops(movie)
    assert [item["kind"] for item in ops] == ["file", "file"], ops
    assert ops[0]["old"] == "Old Title/Old.iso" and ops[0]["new"] == "Old Title/New.iso"
    assert ops[1] == {
        "kind": "file",
        "old": "Old Title/New.iso",
        "new": "New Title (2026) [tmdbid=1]/New.iso",
    }
    assert remaining_ops(movie, {"Old Title/New.iso"}) == [ops[1]]
    assert remaining_ops(movie, {"New Title (2026) [tmdbid=1]/New.iso"}) == []
    print("self-test ok")


ENGINE: Engine | None = None


def start() -> Engine:
    global ENGINE
    if ENGINE is None:
        ENGINE = Engine()
    return ENGINE
