"""Folder+file rename stages, delayed qBittorrent content, and rollback."""

import os
import tempfile

import engine


class _QuietTemp(tempfile.TemporaryDirectory):
    """SQLite keeps the temp db open on Windows, so cleanup must not fail the test."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("ignore_cleanup_errors", True)
        super().__init__(*args, **kwargs)


tempfile.TemporaryDirectory = _QuietTemp


OLD = "穿普拉达的女王2 The Devil Wears Prada 2/The Devil Wears Prada 2.iso"
NEW = "穿普拉达的女王2 (2026) [tmdbid=1314481]/穿普拉达的女王2 (2026).iso"
PLANS = [
    {
        "old_rel": OLD,
        "new_rel": NEW,
        "host_old": "",
        "host_new": "",
    }
]


def test_stages():
    ops = engine.stage_ops(PLANS)
    assert [item["kind"] for item in ops] == ["file", "file"]
    assert ops[0]["old"] == OLD
    assert ops[0]["new"].endswith("/穿普拉达的女王2 (2026).iso")
    assert ops[0]["new"].startswith("穿普拉达的女王2 The Devil Wears Prada 2/")
    assert ops[1]["old"] == ops[0]["new"]
    assert ops[1]["new"] == NEW
    assert engine.stage_ops([{"old_rel": "A/a.iso", "new_rel": "B/a.iso"}]) == [
        {"kind": "folder", "old": "A", "new": "B"}
    ]
    assert engine.stage_ops([{"old_rel": "A/a.iso", "new_rel": "A/b.iso"}]) == [
        {"kind": "file", "old": "A/a.iso", "new": "A/b.iso"}
    ]
    assert engine.remaining_ops(PLANS, {ops[0]["new"]}) == [ops[1]]
    assert engine.remaining_ops(PLANS, {NEW}) == []


class DelayedQbit:
    def __init__(self, names, host_save, delay, move_disk=True):
        self.names = list(names)
        self.host_save = host_save
        self.delay = delay
        self.move_disk = move_disk
        self.pending = []
        self.calls = []
        self.stopped = False
        self.started = False

    def stop(self, _infohash):
        self.stopped = True

    def start(self, _infohash):
        self.started = True

    def files(self, _infohash):
        still = []
        for kind, old, new, left in self.pending:
            if left <= 1:
                self._apply(kind, old, new)
            else:
                still.append((kind, old, new, left - 1))
        self.pending = still
        return [{"name": name} for name in self.names]

    def rename_file(self, _infohash, old, new):
        self.calls.append(("file", old, new))
        self.pending.append(("file", old, new, self.delay))

    def rename_folder(self, _infohash, old, new):
        self.calls.append(("folder", old, new))
        self.pending.append(("folder", old, new, self.delay))

    def _apply(self, kind, old, new):
        if kind == "file":
            if self.move_disk:
                src = os.path.join(self.host_save, old)
                dst = os.path.join(self.host_save, new)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(src):
                    os.rename(src, dst)
            self.names = [new if name == old else name for name in self.names]
            return
        prefix = old + "/"
        self.names = [new + name[len(old) :] if name == old or name.startswith(prefix) else name for name in self.names]
        if not self.move_disk:
            return
        src = os.path.join(self.host_save, old)
        dst = os.path.join(self.host_save, new)
        if os.path.isdir(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)


class StuckQbit:
    def __init__(self, names):
        self.names = list(names)
        self.calls = []
        self.stopped = False
        self.started = False

    def stop(self, _infohash):
        self.stopped = True

    def start(self, _infohash):
        self.started = True

    def files(self, _infohash):
        return [{"name": name} for name in self.names]

    def rename_file(self, _infohash, old, new):
        self.calls.append(("file", old, new))

    def rename_folder(self, _infohash, old, new):
        self.calls.append(("folder", old, new))


def _clock():
    state = {"t": 0}

    def now():
        return state["t"]

    def sleep(seconds):
        state["t"] += seconds or 0

    return now, sleep


def _plans(root):
    host_old = os.path.join(root, OLD)
    host_new = os.path.join(root, NEW)
    os.makedirs(os.path.dirname(host_new), exist_ok=True)
    with open(host_new, "w", encoding="utf-8") as handle:
        handle.write("iso")
    return [
        {
            "old_rel": OLD,
            "new_rel": NEW,
            "host_old": engine.norm_path(host_old),
            "host_new": engine.norm_path(host_new),
        }
    ]


def test_delayed_content_then_success():
    now, sleep = _clock()
    with tempfile.TemporaryDirectory() as tmp:
        plans = _plans(tmp)
        client = DelayedQbit([OLD], tmp, delay=3)
        engine.execute_plans(client, "hash", plans, tmp, timeout=30, pause=1, sleep=sleep, now=now)
        assert client.calls[0][0] == "file"
        assert client.calls[1][0] == "file"
        assert client.calls[1][1] == client.calls[0][2]
        assert client.calls[1][2] == NEW
        assert os.path.isfile(plans[0]["host_new"])
        assert NEW in client.names
        assert not os.path.exists(plans[0]["host_old"])


def test_timeout_restores_filmix_name():
    now, sleep = _clock()
    with tempfile.TemporaryDirectory() as tmp:
        plans = _plans(tmp)
        before = open(plans[0]["host_new"], encoding="utf-8").read()
        client = StuckQbit([OLD])
        try:
            engine.execute_plans(client, "hash", plans, tmp, timeout=3, pause=1, sleep=sleep, now=now)
        except engine.RenameFailed as exc:
            assert "檔名" in str(exc)
        else:
            raise AssertionError("expected timeout")
        assert os.path.isfile(plans[0]["host_new"])
        assert open(plans[0]["host_new"], encoding="utf-8").read() == before
        assert not os.path.exists(plans[0]["host_old"])
        assert client.calls[0][0] == "file"
        assert client.calls[0][1] == OLD
        assert all(kind == "file" for kind, _old, _new in client.calls)
        assert client.stopped
        assert client.started
        assert not os.path.exists(os.path.join(tmp, engine.intermediate_rel(OLD, NEW)))


def test_filmix_final_disk_without_mid():
    """Filmix already at host_new; qBit only updates names. Wait must not require mid on disk."""
    now, sleep = _clock()
    with tempfile.TemporaryDirectory() as tmp:
        plans = _plans(tmp)
        mid = engine.intermediate_rel(OLD, NEW)
        assert mid
        assert os.path.isfile(plans[0]["host_new"])
        assert not os.path.exists(os.path.join(tmp, mid))
        client = DelayedQbit([OLD], tmp, delay=2, move_disk=False)
        mid_path = os.path.join(tmp, mid)

        def rename_file(_infohash, old, new):
            assert not os.path.exists(mid_path)
            DelayedQbit.rename_file(client, _infohash, old, new)
            assert not os.path.exists(mid_path)

        client.rename_file = rename_file
        engine.execute_plans(client, "hash", plans, tmp, timeout=30, pause=1, sleep=sleep, now=now)
        assert [kind for kind, _old, _new in client.calls] == ["file", "file"]
        assert client.calls[0][1] == OLD
        assert client.calls[1][2] == NEW
        assert NEW in client.names
        assert os.path.isfile(plans[0]["host_new"])
        assert open(plans[0]["host_new"], encoding="utf-8").read() == "iso"
        assert not os.path.exists(mid_path)
        assert not os.path.exists(plans[0]["host_old"])
        assert client.stopped
        assert client.started


class FolderNoopQbit:
    """renameFolder returns success but does not update torrent paths (empty dest dir)."""

    def __init__(self, names):
        self.names = list(names)
        self.calls = []
        self.stopped = False
        self.started = False

    def stop(self, _infohash):
        self.stopped = True

    def start(self, _infohash):
        self.started = True

    def files(self, _infohash):
        return [{"name": name} for name in self.names]

    def rename_file(self, _infohash, old, new):
        self.calls.append(("file", old, new))
        self.names = [new if name == old else name for name in self.names]

    def rename_folder(self, _infohash, old, new):
        self.calls.append(("folder", old, new))


class DiskSyncQbit:
    """HTTP 200 always. Downloaded names change only when the destination path exists.

    metadata paths are never-downloaded entries: rename still updates their qB name
    and must not require a file on disk.
    """

    def __init__(self, names, host_save, metadata=None):
        self.names = list(names)
        self.host_save = host_save
        self.metadata = {engine.norm_path(name) for name in (metadata or [])}
        self.calls = []
        self.saw_dest = []
        self.trace = []
        self.stopped = False
        self.started = False

    def stop(self, _infohash):
        self.trace.append("stop")
        self.stopped = True

    def start(self, _infohash):
        self.trace.append("start")
        self.started = True

    def files(self, _infohash):
        return [{"name": name} for name in self.names]

    def _ready(self, new):
        return os.path.lexists(os.path.join(self.host_save, new))

    def _meta(self, name):
        return engine.norm_path(name) in self.metadata

    def rename_file(self, _infohash, old, new):
        assert self.stopped and not self.started
        existed = self._ready(new)
        self.calls.append(("file", old, new))
        self.saw_dest.append(existed)
        self.trace.append("file")
        if old not in self.names:
            return
        if not existed and not self._meta(old):
            return
        self.names = [new if name == old else name for name in self.names]
        if self._meta(old):
            self.metadata.discard(engine.norm_path(old))
            self.metadata.add(engine.norm_path(new))

    def rename_folder(self, _infohash, old, new):
        assert self.stopped and not self.started
        existed = self._ready(new)
        self.calls.append(("folder", old, new))
        self.saw_dest.append(existed)
        self.trace.append("folder")
        prefix = old + "/"
        updated = []
        metadata = set()
        for name in self.names:
            covered = name == old or name.startswith(prefix)
            if covered and (existed or self._meta(name)):
                renamed = new + name[len(old) :]
                updated.append(renamed)
                if self._meta(name):
                    metadata.add(engine.norm_path(renamed))
            else:
                updated.append(name)
                if self._meta(name):
                    metadata.add(engine.norm_path(name))
        self.names = updated
        self.metadata = metadata

    def rename_torrent(self, _infohash, _name):
        raise AssertionError("torrent Title must stay unchanged")


def test_folder_fallback_to_rename_file():
    now, sleep = _clock()
    with tempfile.TemporaryDirectory() as tmp:
        plans = []
        names = []
        for name in ("a.mkv", "b.mkv"):
            old_rel = f"Old/{name}"
            new_rel = f"New/{name}"
            host_old = os.path.join(tmp, old_rel)
            host_new = os.path.join(tmp, new_rel)
            os.makedirs(os.path.dirname(host_new), exist_ok=True)
            with open(host_new, "w", encoding="utf-8") as handle:
                handle.write(name)
            plans.append(
                {
                    "old_rel": old_rel,
                    "new_rel": new_rel,
                    "host_old": engine.norm_path(host_old),
                    "host_new": engine.norm_path(host_new),
                }
            )
            names.append(old_rel)
        client = FolderNoopQbit(names)
        engine.execute_plans(client, "hash", plans, tmp, timeout=3, pause=1, sleep=sleep, now=now)
        assert client.calls[0] == ("folder", "Old", "New")
        assert ("file", "Old/a.mkv", "New/a.mkv") in client.calls
        assert ("file", "Old/b.mkv", "New/b.mkv") in client.calls
        assert "New/a.mkv" in client.names
        assert "New/b.mkv" in client.names
        assert os.path.isfile(plans[0]["host_new"])
        assert os.path.isfile(plans[1]["host_new"])
        assert client.stopped
        assert client.started


def _run(client, plans, root, **kwargs):
    now, sleep = _clock()
    engine.execute_plans(client, "hash", plans, root, timeout=30, pause=1, sleep=sleep, now=now, **kwargs)


def test_disk_mv_then_api_sync():
    """Bytes still at the old path. SeedBridge must mv before qBit will sync names."""
    with tempfile.TemporaryDirectory() as tmp:
        host_old = os.path.join(tmp, OLD)
        host_new = os.path.join(tmp, NEW)
        os.makedirs(os.path.dirname(host_old), exist_ok=True)
        with open(host_old, "w", encoding="utf-8") as handle:
            handle.write("iso")
        plans = [
            {
                "old_rel": OLD,
                "new_rel": NEW,
                "host_old": engine.norm_path(host_old),
                "host_new": engine.norm_path(host_new),
            }
        ]
        mid = engine.intermediate_rel(OLD, NEW)
        client = DiskSyncQbit([OLD], tmp)
        _run(client, plans, tmp)
        assert client.trace[0] == "stop"
        assert client.trace[-1] == "start"
        assert [kind for kind, _old, _new in client.calls] == ["file", "file"]
        assert client.calls[0] == ("file", OLD, mid)
        assert client.calls[1] == ("file", mid, NEW)
        assert client.saw_dest == [True, True]
        assert client.names == [NEW]
        assert os.path.isfile(host_new)
        assert open(host_new, encoding="utf-8").read() == "iso"
        assert not os.path.exists(host_old)
        assert not os.path.exists(os.path.join(tmp, mid))


def test_disk_mv_folder_then_api():
    with tempfile.TemporaryDirectory() as tmp:
        plans = []
        names = []
        for name in ("a.mkv", "b.mkv"):
            old_rel = f"Old/{name}"
            new_rel = f"New/{name}"
            host_old = os.path.join(tmp, old_rel)
            os.makedirs(os.path.dirname(host_old), exist_ok=True)
            with open(host_old, "w", encoding="utf-8") as handle:
                handle.write(name)
            plans.append(
                {
                    "old_rel": old_rel,
                    "new_rel": new_rel,
                    "host_old": engine.norm_path(host_old),
                    "host_new": engine.norm_path(os.path.join(tmp, new_rel)),
                }
            )
            names.append(old_rel)
        client = DiskSyncQbit(names, tmp)
        _run(client, plans, tmp)
        assert client.calls == [("folder", "Old", "New")]
        assert client.saw_dest == [True]
        assert client.names == ["New/a.mkv", "New/b.mkv"]
        assert not os.path.isdir(os.path.join(tmp, "Old"))
        assert open(plans[0]["host_new"], encoding="utf-8").read() == "a.mkv"
        assert open(plans[1]["host_new"], encoding="utf-8").read() == "b.mkv"
        assert client.trace[0] == "stop"
        assert client.trace[-1] == "start"


def test_api_syncs_when_filmix_already_placed():
    """Dest folder already holds the files, so the API syncs with no extra mv."""
    with tempfile.TemporaryDirectory() as tmp:
        plans = []
        names = []
        for name in ("a.mkv", "b.mkv"):
            old_rel = f"Old/{name}"
            new_rel = f"New/{name}"
            host_new = os.path.join(tmp, new_rel)
            os.makedirs(os.path.dirname(host_new), exist_ok=True)
            with open(host_new, "w", encoding="utf-8") as handle:
                handle.write(name)
            plans.append(
                {
                    "old_rel": old_rel,
                    "new_rel": new_rel,
                    "host_old": engine.norm_path(os.path.join(tmp, old_rel)),
                    "host_new": engine.norm_path(host_new),
                }
            )
            names.append(old_rel)
        client = DiskSyncQbit(names, tmp)
        _run(client, plans, tmp)
        assert client.calls == [("folder", "Old", "New")]
        assert client.saw_dest == [True]
        assert client.names == ["New/a.mkv", "New/b.mkv"]
        assert not os.path.exists(os.path.join(tmp, "Old"))
        assert open(plans[0]["host_new"], encoding="utf-8").read() == "a.mkv"
        assert client.started


def test_same_folder_file_api_when_filmix_placed():
    with tempfile.TemporaryDirectory() as tmp:
        old_rel = "Dir/a.iso"
        new_rel = "Dir/b.iso"
        host_new = os.path.join(tmp, new_rel)
        os.makedirs(os.path.dirname(host_new), exist_ok=True)
        with open(host_new, "w", encoding="utf-8") as handle:
            handle.write("iso")
        plans = [
            {
                "old_rel": old_rel,
                "new_rel": new_rel,
                "host_old": engine.norm_path(os.path.join(tmp, old_rel)),
                "host_new": engine.norm_path(host_new),
            }
        ]
        client = DiskSyncQbit([old_rel], tmp)
        _run(client, plans, tmp, resume=False)
        assert client.calls == [("file", old_rel, new_rel)]
        assert client.saw_dest == [True]
        assert client.names == [new_rel]
        assert open(host_new, encoding="utf-8").read() == "iso"
        assert not os.path.exists(os.path.join(tmp, old_rel))
        assert client.stopped
        assert not client.started


def test_ensure_disk_stays_inside_save():
    with tempfile.TemporaryDirectory() as tmp:
        save = os.path.join(tmp, "save")
        outside = os.path.join(tmp, "outside.iso")
        os.makedirs(save)
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("x")
        inside = os.path.join(save, "movie.iso")
        with open(inside, "w", encoding="utf-8") as handle:
            handle.write("iso")
        engine._ensure_disk_at(save, [outside], "kept/movie.iso")
        assert os.path.isfile(outside)
        assert not os.path.exists(os.path.join(save, "kept", "movie.iso"))
        engine._ensure_disk_at(save, [save], "child")
        assert os.path.isdir(save)
        assert os.path.isfile(inside)
        try:
            engine._ensure_disk_at(save, [inside], "../escaped.iso")
        except engine.RenameFailed:
            pass
        else:
            raise AssertionError("dest outside save must be refused")
        assert os.path.isfile(inside)
        assert not os.path.exists(os.path.join(tmp, "escaped.iso"))
        sibling = os.path.join(tmp, "save_extra", "nope.iso")
        os.makedirs(os.path.dirname(sibling), exist_ok=True)
        with open(sibling, "w", encoding="utf-8") as handle:
            handle.write("no")
        engine._ensure_disk_at(save, [sibling], "nope.iso")
        assert os.path.isfile(sibling)
        assert not os.path.exists(os.path.join(save, "nope.iso"))


def _title_case(tmp: str):
    old_rel = "Pete@HDSky/movie.iso"
    new_rel = "Movie (2026) [tmdbid=1]/movie.iso"
    host_new = os.path.join(tmp, new_rel)
    os.makedirs(os.path.dirname(host_new), exist_ok=True)
    with open(host_new, "w", encoding="utf-8") as handle:
        handle.write("iso")
    plans = [
        {
            "old_rel": old_rel,
            "new_rel": new_rel,
            "host_old": engine.norm_path(os.path.join(tmp, old_rel)),
            "host_new": engine.norm_path(host_new),
        }
    ]
    files = [{"name": old_rel}]
    return old_rel, new_rel, host_new, plans, files


def test_apply_leaves_torrent_title():
    """Title rename stays off unless rename_torrent_title is set."""
    with tempfile.TemporaryDirectory() as tmp:
        old_rel, new_rel, host_new, plans, files = _title_case(tmp)
        client = DiskSyncQbit([old_rel], tmp)
        engine.db.init(os.path.join(tmp, "data"))
        assert engine.db.get_settings().get("rename_torrent_title") is False
        engine.Engine()._apply(client, "hash", "Pete@HDSky", plans, files, tmp, resume=False)
        assert client.calls == [("folder", "Pete@HDSky", "Movie (2026) [tmdbid=1]")]
        assert client.names == [new_rel]
        assert open(host_new, encoding="utf-8").read() == "iso"
        assert not client.started


def test_apply_renames_torrent_title_when_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        old_rel, new_rel, host_new, plans, files = _title_case(tmp)
        client = DiskSyncQbit([old_rel], tmp)
        titles = []

        def rename_torrent(infohash, title):
            titles.append((infohash, title))

        client.rename_torrent = rename_torrent
        engine.db.init(os.path.join(tmp, "data"))
        stored = engine.db.get_settings()
        stored["rename_torrent_title"] = True
        engine.db.save_settings(stored)
        assert engine.db.get_settings()["rename_torrent_title"] is True
        engine.Engine()._apply(client, "hash", "Pete@HDSky", plans, files, tmp, resume=False)
        assert titles == [("hash", "Movie (2026) [tmdbid=1]")]
        assert client.names == [new_rel]
        assert open(host_new, encoding="utf-8").read() == "iso"


def test_skips_missing_sibling_under_folder_rename():
    """A never-downloaded file must not fail the folder rename of files that exist."""
    with tempfile.TemporaryDirectory() as tmp:
        present_old = "新特警判官.2012/新特警判官.mkv"
        present_new = "新特警判官 (2012) [tmdbid=1]/新特警判官.mkv"
        missing_old = "新特警判官.2012/新特警判官.jpg"
        missing_new = "新特警判官 (2012) [tmdbid=1]/新特警判官.jpg"
        host_new = os.path.join(tmp, present_new)
        os.makedirs(os.path.dirname(host_new), exist_ok=True)
        with open(host_new, "w", encoding="utf-8") as handle:
            handle.write("mkv")
        plans = [
            {
                "old_rel": present_old,
                "new_rel": present_new,
                "host_old": engine.norm_path(os.path.join(tmp, present_old)),
                "host_new": engine.norm_path(host_new),
            },
            {
                "old_rel": missing_old,
                "new_rel": missing_new,
                "host_old": engine.norm_path(os.path.join(tmp, missing_old)),
                "host_new": engine.norm_path(os.path.join(tmp, missing_new)),
            },
        ]
        client = DiskSyncQbit([present_old, missing_old], tmp, metadata=[missing_old])
        _run(client, plans, tmp)
        assert ("folder", "新特警判官.2012", "新特警判官 (2012) [tmdbid=1]") in client.calls
        assert present_new in client.names
        assert missing_new in client.names
        assert os.path.isfile(host_new)
        assert open(host_new, encoding="utf-8").read() == "mkv"
        assert not os.path.exists(os.path.join(tmp, missing_old))
        assert not os.path.exists(os.path.join(tmp, missing_new))


FALLOUT_OLD = "Fallout/Fallout.S02E01-1080p-Amazon.WEB-DL-AVC-DDP 5.1.mkv"
FALLOUT_NEW_NAME = "辐射 (2024) S02E01-1080p-Amazon.WEB-DL-AVC-DDP 5.1-[tmdbid=106379].mkv"
FALLOUT_SHOW = "辐射 (2024) [tmdbid=106379]"
FALLOUT_FLAT = f"{FALLOUT_SHOW}/{FALLOUT_NEW_NAME}"
FALLOUT_SEASON = f"{FALLOUT_SHOW}/Season 2/{FALLOUT_NEW_NAME}"
FALLOUT_HOPS = [
    FALLOUT_OLD,
    f"Fallout/{FALLOUT_NEW_NAME}",
    f"Fallout/Season 2/{FALLOUT_NEW_NAME}",
    FALLOUT_SEASON,
]


def _deeper_and_rehomes(old: str, new: str) -> bool:
    old_segs = [part for part in old.split("/") if part]
    new_segs = [part for part in new.split("/") if part]
    return len(new_segs) > len(old_segs) and bool(old_segs) and bool(new_segs) and old_segs[0] != new_segs[0]


def _rel_plans(root, old_rel, new_rel):
    return [
        {
            "old_rel": old_rel,
            "new_rel": new_rel,
            "host_old": engine.norm_path(os.path.join(root, old_rel)),
            "host_new": engine.norm_path(os.path.join(root, new_rel)),
        }
    ]


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class CollapsingQbit:
    """Names always update. Adding a directory while changing the root keeps only the last directory."""

    def __init__(self, names):
        self.names = list(names)
        self.calls = []
        self.stopped = False
        self.started = False

    def stop(self, _infohash):
        self.stopped = True

    def start(self, _infohash):
        self.started = True

    def files(self, _infohash):
        return [{"name": name} for name in self.names]

    def _store(self, old, new):
        if _deeper_and_rehomes(old, new):
            parts = [part for part in new.split("/") if part]
            return "/".join(parts[-2:])
        return new

    def rename_file(self, _infohash, old, new):
        self.calls.append(("file", old, new))
        if old not in self.names:
            return
        stored = self._store(old, new)
        self.names = [stored if name == old else name for name in self.names]

    def rename_folder(self, _infohash, old, new):
        self.calls.append(("folder", old, new))
        prefix = old + "/"
        self.names = [new + name[len(old) :] if name == old or name.startswith(prefix) else name for name in self.names]


def test_flat_fallout_stays_two_file_steps():
    plans = [{"old_rel": FALLOUT_OLD, "new_rel": FALLOUT_FLAT}]
    ops = engine.stage_ops(plans)
    assert [item["kind"] for item in ops] == ["file", "file"]
    assert ops[1]["new"] == FALLOUT_FLAT
    assert ops[1]["old"].startswith("Fallout/")
    assert all("Season" not in item["old"] and "Season" not in item["new"] for item in ops)
    assert engine.intermediate_rel(FALLOUT_OLD, FALLOUT_FLAT) == ops[0]["new"]
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, FALLOUT_OLD), "mkv")
        plans = _rel_plans(tmp, FALLOUT_OLD, FALLOUT_FLAT)
        client = DiskSyncQbit([FALLOUT_OLD], tmp)
        _run(client, plans, tmp)
        assert [kind for kind, _old, _new in client.calls] == ["file", "file"]
        assert client.calls[1][2] == FALLOUT_FLAT
        assert client.names == [FALLOUT_FLAT]
        assert client.saw_dest == [True, True]
        assert open(plans[0]["host_new"], encoding="utf-8").read() == "mkv"
        assert not os.path.exists(plans[0]["host_old"])


def test_season_layer_inserts_one_directory_at_a_time():
    plans = [{"old_rel": FALLOUT_OLD, "new_rel": FALLOUT_SEASON}]
    assert engine.plan_hops(FALLOUT_OLD, FALLOUT_SEASON) == FALLOUT_HOPS
    mid = engine.intermediate_rel(FALLOUT_OLD, FALLOUT_SEASON)
    assert mid == f"Fallout/{FALLOUT_NEW_NAME}"
    assert "Season" not in mid
    ops = engine.stage_ops(plans)
    assert ops[-1]["new"] == FALLOUT_SEASON
    assert [item["kind"] for item in ops] == ["file", "file", "file"]
    assert [(item["old"], item["new"]) for item in ops] == list(zip(FALLOUT_HOPS, FALLOUT_HOPS[1:]))
    assert not any(_deeper_and_rehomes(item["old"], item["new"]) for item in ops)
    same_root = engine.stage_ops(
        [{"old_rel": "Fallout/episode.mkv", "new_rel": "Fallout/Season 2/episode.mkv"}]
    )
    assert same_root == [{"kind": "file", "old": "Fallout/episode.mkv", "new": "Fallout/Season 2/episode.mkv"}]


def test_season_disk_mv_ends_at_full_path():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, FALLOUT_OLD), "mkv")
        plans = _rel_plans(tmp, FALLOUT_OLD, FALLOUT_SEASON)
        client = DiskSyncQbit([FALLOUT_OLD], tmp)
        _run(client, plans, tmp)
        assert client.names == [FALLOUT_SEASON]
        assert client.saw_dest == [True, True, True]
        assert not any(_deeper_and_rehomes(old, new) for _kind, old, new in client.calls)
        assert open(plans[0]["host_new"], encoding="utf-8").read() == "mkv"
        assert not os.path.exists(plans[0]["host_old"])
        for hop in FALLOUT_HOPS[1:-1]:
            assert not os.path.exists(os.path.join(tmp, hop))


def test_season_filmix_final_does_not_rebuild_hops():
    now, sleep = _clock()
    with tempfile.TemporaryDirectory() as tmp:
        plans = _rel_plans(tmp, FALLOUT_OLD, FALLOUT_SEASON)
        _write(plans[0]["host_new"], "mkv")
        client = DelayedQbit([FALLOUT_OLD], tmp, delay=2, move_disk=False)
        engine.execute_plans(client, "hash", plans, tmp, timeout=30, pause=1, sleep=sleep, now=now)
        assert client.names == [FALLOUT_SEASON]
        assert client.calls[-1][2] == FALLOUT_SEASON
        assert open(plans[0]["host_new"], encoding="utf-8").read() == "mkv"
        for hop in FALLOUT_HOPS[1:-1]:
            assert not os.path.exists(os.path.join(tmp, hop))


def test_collapsing_client_keeps_full_season_path():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, FALLOUT_OLD), "mkv")
        plans = _rel_plans(tmp, FALLOUT_OLD, FALLOUT_SEASON)
        client = CollapsingQbit([FALLOUT_OLD])
        _run(client, plans, tmp)
        assert client.names == [FALLOUT_SEASON]
        assert not any(_deeper_and_rehomes(old, new) for _kind, old, new in client.calls)
        assert open(plans[0]["host_new"], encoding="utf-8").read() == "mkv"
    with tempfile.TemporaryDirectory() as tmp:
        bare = CollapsingQbit([FALLOUT_OLD])
        _run(bare, _rel_plans(tmp, FALLOUT_OLD, FALLOUT_SEASON), tmp)
        assert bare.names == [FALLOUT_SEASON]
        assert not any(_deeper_and_rehomes(old, new) for _kind, old, new in bare.calls)


def test_rematch_collapsed_season_path():
    collapsed = f"Season 2/{FALLOUT_NEW_NAME}"
    with tempfile.TemporaryDirectory() as tmp:
        plans = _rel_plans(tmp, FALLOUT_OLD, FALLOUT_SEASON)
        _write(plans[0]["host_new"], "mkv")
        direct = FolderNoopQbit([collapsed])
        _run(direct, plans, tmp)
        assert direct.names == [FALLOUT_SEASON]
        assert ("file", collapsed, FALLOUT_SEASON) in direct.calls
        assert open(plans[0]["host_new"], encoding="utf-8").read() == "mkv"
        assert not os.path.exists(os.path.join(tmp, FALLOUT_SHOW, FALLOUT_NEW_NAME))

        plans = _rel_plans(tmp, FALLOUT_OLD, FALLOUT_SEASON)
        collapsing = CollapsingQbit([collapsed])
        _run(collapsing, plans, tmp)
        assert collapsing.names == [FALLOUT_SEASON]
        assert ("file", collapsed, FALLOUT_SEASON) in collapsing.calls
        assert ("file", collapsed, f"{FALLOUT_SHOW}/{FALLOUT_NEW_NAME}") in collapsing.calls
        assert ("file", f"{FALLOUT_SHOW}/{FALLOUT_NEW_NAME}", FALLOUT_SEASON) in collapsing.calls
        assert open(plans[0]["host_new"], encoding="utf-8").read() == "mkv"
        assert not os.path.exists(os.path.join(tmp, FALLOUT_SHOW, FALLOUT_NEW_NAME))


def test_choose_new_host_finds_season_layer():
    with tempfile.TemporaryDirectory() as tmp:
        root = engine.norm_path(tmp)
        filename = FALLOUT_NEW_NAME
        old_name = "Fallout.S02E01-1080p-Amazon.WEB-DL-AVC-DDP 5.1.mkv"
        season_dir = os.path.join(tmp, FALLOUT_SHOW, "Season 2")
        os.makedirs(season_dir)
        with open(os.path.join(season_dir, filename), "w", encoding="utf-8") as handle:
            handle.write("mkv")
        old_file = f"{root}/Fallout/{old_name}"
        events = [
            {
                "src": f"{root}/Fallout/{old_name}",
                "dst": f"{root}/{FALLOUT_SHOW}/{filename}",
                "is_dir": False,
            },
            {"src": f"{root}/Fallout", "dst": f"{root}/{FALLOUT_SHOW}", "is_dir": True},
        ]
        chosen = engine.choose_new_host(old_file, events)
        assert chosen == f"{root}/{FALLOUT_SHOW}/Season 2/{filename}", chosen
        with open(os.path.join(tmp, FALLOUT_SHOW, filename), "w", encoding="utf-8") as handle:
            handle.write("flat")
        chosen = engine.choose_new_host(old_file, events)
        assert chosen == f"{root}/{FALLOUT_SHOW}/{filename}", chosen


def test_only_missing_files_do_not_raise():
    with tempfile.TemporaryDirectory() as tmp:
        old_rel = "Old/skip.jpg"
        new_rel = "New/skip.jpg"
        plans = [
            {
                "old_rel": old_rel,
                "new_rel": new_rel,
                "host_old": engine.norm_path(os.path.join(tmp, old_rel)),
                "host_new": engine.norm_path(os.path.join(tmp, new_rel)),
            }
        ]
        engine.align_disk_to_qbit(plans, {old_rel}, tmp)
        client = DiskSyncQbit([old_rel], tmp, metadata=[old_rel])
        _run(client, plans, tmp)
        assert ("folder", "Old", "New") in client.calls
        assert client.names == [new_rel]
        assert not os.path.exists(os.path.join(tmp, old_rel))
        assert not os.path.exists(os.path.join(tmp, new_rel))


if __name__ == "__main__":
    test_stages()
    test_delayed_content_then_success()
    test_timeout_restores_filmix_name()
    test_filmix_final_disk_without_mid()
    test_folder_fallback_to_rename_file()
    test_disk_mv_then_api_sync()
    test_disk_mv_folder_then_api()
    test_api_syncs_when_filmix_already_placed()
    test_same_folder_file_api_when_filmix_placed()
    test_ensure_disk_stays_inside_save()
    test_apply_leaves_torrent_title()
    test_apply_renames_torrent_title_when_enabled()
    test_skips_missing_sibling_under_folder_rename()
    test_flat_fallout_stays_two_file_steps()
    test_season_layer_inserts_one_directory_at_a_time()
    test_season_disk_mv_ends_at_full_path()
    test_season_filmix_final_does_not_rebuild_hops()
    test_collapsing_client_keeps_full_season_path()
    test_rematch_collapsed_season_path()
    test_choose_new_host_finds_season_layer()
    test_only_missing_files_do_not_raise()
    engine.self_test()
    print("test_rename ok")
