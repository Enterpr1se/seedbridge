"""Folder+file rename stages, delayed qBittorrent content, and rollback."""

import os
import tempfile

import engine


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
        engine.execute_plans(client, "hash", plans, tmp, timeout=30, pause=1, sleep=sleep, now=now)
        assert [kind for kind, _old, _new in client.calls] == ["file", "file"]
        assert client.calls[0][1] == OLD
        assert client.calls[1][2] == NEW
        assert NEW in client.names
        assert os.path.isfile(plans[0]["host_new"])
        assert open(plans[0]["host_new"], encoding="utf-8").read() == "iso"
        assert not os.path.exists(os.path.join(tmp, mid))
        assert not os.path.exists(plans[0]["host_old"])


class FolderNoopQbit:
    """renameFolder returns success but does not update torrent paths (empty dest dir)."""

    def __init__(self, names):
        self.names = list(names)
        self.calls = []

    def files(self, _infohash):
        return [{"name": name} for name in self.names]

    def rename_file(self, _infohash, old, new):
        self.calls.append(("file", old, new))
        self.names = [new if name == old else name for name in self.names]

    def rename_folder(self, _infohash, old, new):
        self.calls.append(("folder", old, new))


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


if __name__ == "__main__":
    test_stages()
    test_delayed_content_then_success()
    test_timeout_restores_filmix_name()
    test_filmix_final_disk_without_mid()
    test_folder_fallback_to_rename_file()
    engine.self_test()
    print("test_rename ok")
