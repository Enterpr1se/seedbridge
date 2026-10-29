"""Watch-folder filter and Telegram caption rules."""

import json
import os
import tempfile
import threading
import urllib.request

import db
import engine
import notify

FORBIDDEN = ("评分", "評分", "类型", "類型", "媒体库", "媒體庫", "TMDB ID", "IMDB ID", "时间", "時間", "剧情", "劇情", "新入库")


class _Body:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def _event(src, dst):
    return {
        "data": [
            {
                "action": "rename",
                "source_file": src,
                "destination_file": dst,
                "is_dir": False,
            }
        ]
    }


class _QuietTemp(tempfile.TemporaryDirectory):
    """SQLite keeps the temp db open on Windows, so cleanup must not fail the test."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("ignore_cleanup_errors", True)
        super().__init__(*args, **kwargs)


tempfile.TemporaryDirectory = _QuietTemp


def _prepare(tmp, watch):
    db.init(tmp)
    stored = db.get_settings()
    stored["maps"] = [{"cd_prefix": "/115/Video", "local_prefix": "/media", "qbit_prefix": "/downloads"}]
    stored["watch_folders"] = watch
    stored["enabled"] = False
    stored["debounce_sec"] = 60
    db.save_settings(stored)


def test_watch_empty_still_processes():
    with tempfile.TemporaryDirectory() as tmp:
        _prepare(tmp, "")
        added = engine.Engine().add_webhook(_event("/115/Video/Other/a.mkv", "/115/Video/Other/b.mkv"))
        assert added == 1
        assert any(item["status"] == "recv" for item in db.list_activity())


def test_watch_filters_outside_paths():
    with tempfile.TemporaryDirectory() as tmp:
        _prepare(tmp, "/115/Video/Movie;/media/TV")
        bridge = engine.Engine()
        inside = bridge.add_webhook(_event("/115/Video/Movie/a.mkv", "/115/Video/Movie/b.mkv"))
        outside = bridge.add_webhook(_event("/115/Video/Other/a.mkv", "/115/Video/Other/b.mkv"))
        local_tv = bridge.add_webhook(_event("/115/Video/TV/a.mkv", "/115/Video/TV/b.mkv"))
        assert inside == 1
        assert local_tv == 1
        assert outside == 0
        rows = db.list_activity()
        assert any("/115/Video/Movie/a.mkv" == item["old_path"] for item in rows)
        assert not any("/115/Video/Other/a.mkv" == item["old_path"] for item in rows)
        assert not engine.path_watched({"watch_folders": "/115/Video/Movie"}, "/other/a", "/other/b", None, None)


def test_success_caption():
    text = notify.success_text("Show", "Old/a.mkv", "New [tmdbid=1]/a.mkv")
    assert "已成功改名" in text
    assert "Old/a.mkv" in text
    assert "New [tmdbid=1]/a.mkv" in text
    for word in FORBIDDEN:
        assert word not in text


def test_failure_caption():
    text = notify.failure_text("Show.mkv", "/media/Old/a.mkv", "/media/New/a.mkv", "qBittorrent 未完成改名")
    assert "/media/Old/a.mkv" in text
    assert "/media/New/a.mkv" in text
    assert "Show.mkv" in text
    for word in FORBIDDEN:
        assert word not in text
    dirty = notify.failure_text("a.mkv", "/old", "/new", "评分 9 剧情很长 TMDB ID 1 新入库")
    for word in ("评分", "剧情", "TMDB ID", "新入库"):
        assert word not in dirty
    assert "/old" in dirty


def test_telegram_send_respects_token():
    calls = []

    def urlopen(request, timeout=20):
        calls.append(request)
        return _Body(b"{}")

    settings = {"telegram_bot_token": "123:ABC", "telegram_chat_id": "42", "tmdb_api_key": ""}
    assert notify.notify_now("ok", "Show", "a.mkv", "b.mkv", settings=settings, urlopen=urlopen)
    assert len(calls) == 1
    assert calls[0].full_url.endswith("/sendMessage")
    body = json.loads(calls[0].data.decode("utf-8"))
    assert "已成功改名" in body["text"]
    assert notify.notify_now("ok", "Show", "a.mkv", "b.mkv", settings={"telegram_bot_token": "", "telegram_chat_id": "42"}, urlopen=urlopen) is False
    assert len(calls) == 1


def test_disk_poster_and_tmdb_fallback():
    calls = []

    def urlopen(request, timeout=20):
        calls.append(request)
        url = request.full_url
        if "api.themoviedb.org" in url:
            return _Body(b'{"poster_path":"/abc.jpg"}')
        if "image.tmdb.org" in url:
            return _Body(b"\xff\xd8\xff")
        return _Body(b"{}")

    with tempfile.TemporaryDirectory() as tmp:
        poster = os.path.join(tmp, "Poster.JPG")
        with open(poster, "wb") as handle:
            handle.write(b"jpeg")
        settings = {"telegram_bot_token": "123:ABC", "telegram_chat_id": "42", "tmdb_api_key": "secret-key"}
        assert notify.notify_now(
            "ok",
            "Show",
            "old.mkv",
            "Movie [tmdbid=9]/a.mkv",
            poster_dir=tmp,
            settings=settings,
            urlopen=urlopen,
        )
        assert calls[0].full_url.endswith("/sendPhoto")
        assert "已成功改名".encode() in calls[0].data
        assert b"secret-key" not in calls[0].data
        calls.clear()
        assert notify.notify_now(
            "ok",
            "Show",
            "old.mkv",
            "Movie [tmdbid=9]/a.mkv",
            poster_dir=os.path.join(tmp, "missing"),
            settings=settings,
            urlopen=urlopen,
        )
        hosts = [item.full_url for item in calls]
        assert any("api.themoviedb.org" in host for host in hosts)
        assert any(host.endswith("/sendPhoto") for host in hosts)
        assert all(b"secret-key" not in (item.data or b"") for item in calls if "api.telegram.org" in item.full_url)


def test_no_tmdb_without_key():
    calls = []

    def urlopen(request, timeout=20):
        calls.append(request)
        return _Body(b"{}")

    saved = os.environ.pop("TMDB_API_KEY", None)
    try:
        assert notify.notify_now(
            "ok",
            "Show",
            "Old/a.mkv",
            "Movie [tmdbid=9]/a.mkv",
            poster_dir="/no/such/folder",
            settings={"telegram_bot_token": "123:ABC", "telegram_chat_id": "42", "tmdb_api_key": ""},
            urlopen=urlopen,
        )
    finally:
        if saved is not None:
            os.environ["TMDB_API_KEY"] = saved
    assert len(calls) == 1
    assert calls[0].full_url.endswith("/sendMessage")
    assert "themoviedb.org" not in calls[0].full_url
    body = json.loads(calls[0].data.decode("utf-8"))
    assert "已成功改名" in body["text"]
    for word in FORBIDDEN:
        assert word not in body["text"]


def test_secrets_stay_masked():
    with tempfile.TemporaryDirectory() as tmp:
        db.init(tmp)
        stored = db.get_settings()
        stored["telegram_bot_token"] = "123:SECRET"
        stored["telegram_chat_id"] = "99"
        stored["tmdb_api_key"] = "tmdb-secret"
        saved = db.save_settings(stored)
        assert saved["telegram_bot_token"] == "123:SECRET"
        blank = db.save_settings({"telegram_bot_token": "", "tmdb_api_key": "", "telegram_chat_id": "99"})
        assert blank["telegram_bot_token"] == "123:SECRET"
        assert blank["tmdb_api_key"] == "tmdb-secret"
        public = db.public_settings(blank)
        assert public["telegram_bot_token"] == ""
        assert public["tmdb_api_key"] == ""
        assert public["telegram_token_set"] is True
        assert public["tmdb_api_key_set"] is True
        assert "123:SECRET" not in json.dumps(public)
        assert "tmdb-secret" not in json.dumps(public)


def _telegram(calls):
    return [item for item in calls if "api.telegram.org" in getattr(item, "full_url", "")]


def _message_text(request) -> str:
    if request.full_url.endswith("/sendMessage"):
        return json.loads(request.data.decode("utf-8"))["text"]
    return request.data.decode("utf-8", "replace")


def _use_urlopen(calls):
    original = urllib.request.urlopen

    def urlopen(request, timeout=20):
        calls.append(request)
        url = getattr(request, "full_url", "")
        if "api.themoviedb.org" in url:
            return _Body(b'{"poster_path":"/abc.jpg"}')
        if "image.tmdb.org" in url:
            return _Body(b"\xff\xd8\xff")
        return _Body(b"{}")

    urllib.request.urlopen = urlopen
    return original


def _restore_urlopen(original):
    urllib.request.urlopen = original


def _drain_telegram():
    for thread in threading.enumerate():
        if thread.name.startswith("telegram"):
            thread.join(timeout=5)


def _arm(tmp, **extra):
    db.init(tmp)
    stored = db.get_settings()
    stored["maps"] = [{"cd_prefix": "/115/Video", "local_prefix": "/media", "qbit_prefix": "/downloads"}]
    stored["enabled"] = True
    stored["dry_run"] = False
    stored["watch_folders"] = ""
    stored["telegram_bot_token"] = "123:ABC"
    stored["telegram_chat_id"] = "42"
    stored["tmdb_api_key"] = ""
    stored.update(extra)
    db.save_settings(stored)


class _FakeQbit:
    def __init__(self, settings, timeout=8):
        pass

    def login(self):
        return None

    def torrents(self):
        return [{
            "hash": "abc123def",
            "name": "Show",
            "state": "uploading",
            "save_path": "/downloads/Show",
        }]

    def files(self, _infohash):
        return [{"name": "Old/a.mkv"}, {"name": "Old/b.mkv"}, {"name": "Old/c.mkv"}]

    def torrent(self, _infohash):
        return {"state": "uploading"}

    def stop(self, _infohash):
        return None

    def start(self, _infohash):
        return None


def _folder_batch():
    return [{
        "src": "/media/Show/Old",
        "dst": "/media/Show/New",
        "is_dir": True,
        "raw_src": "/115/Video/Show/Old",
        "raw_dst": "/115/Video/Show/New",
    }]


def test_job_text_counts_and_failure_limit():
    ten = [f"/media/zz-fail-{index}.mkv" for index in range(10)]
    listed = notify.job_text("Show", ok=[{"new": "New/a.mkv"}], failed=[{"path": path} for path in ten])
    assert "已成功改名" in listed
    assert "成功 1 個" in listed
    assert "失敗 10 個" in listed
    for path in ten:
        assert path in listed
    eleven = ten + ["/media/zz-fail-extra.mkv"]
    hidden = notify.job_text(
        "Show",
        ok=[{"new": "New/a.mkv"}],
        failed=[{"path": path} for path in eleven],
        detail="zz-fail-0.mkv 评分 9 剧情",
    )
    assert "已成功改名" in hidden
    assert "成功 1 個" in hidden
    assert "失敗 11 個" in hidden
    assert "SeedBridge" in hidden and "紀錄" in hidden
    for path in eleven:
        assert path not in hidden
    assert "zz-fail" not in hidden
    for word in FORBIDDEN:
        assert word not in hidden
    failure = notify.job_text(
        "Show",
        failed=[{"path": "/media/Old/a.mkv"}, {"path": "/media/Old/b.mkv"}],
        detail="评分 9 剧情很长 TMDB ID 1 类型 电影 媒体库 时间 新入库 IMDB ID tt1 評分 類型 媒體庫 時間 劇情",
    )
    assert "已成功改名" not in failure
    assert "改名失敗" in failure
    assert "成功 0 個" in failure
    assert "失敗 2 個" in failure
    assert "/media/Old/a.mkv" in failure
    assert "/media/Old/b.mkv" in failure
    for word in FORBIDDEN:
        assert word not in failure
    assert notify.job_text("", ok=[], failed=[], detail="") == "改名失敗\n成功 0 個，失敗 0 個"


def test_job_one_telegram_call():
    calls = []

    def urlopen(request, timeout=20):
        calls.append(request)
        return _Body(b"{}")

    settings = {"telegram_bot_token": "123:ABC", "telegram_chat_id": "42", "tmdb_api_key": ""}
    ok = [{"old": f"Old/{index}.mkv", "new": f"New/{index}.mkv"} for index in range(4)]
    failed = [{"path": "/media/zz-bad-a.mkv"}, {"path": "/media/zz-bad-b.mkv"}]
    assert notify.notify_job("Show", ok=ok, failed=failed, settings=settings, urlopen=urlopen)
    assert notify.notify_job("Show", settings={"telegram_bot_token": "", "telegram_chat_id": "42"}, urlopen=urlopen) is False
    assert len(calls) == 1
    assert calls[0].full_url.endswith("/sendMessage")
    text = _message_text(calls[0])
    assert "已成功改名" in text
    assert "成功 4 個" in text
    assert "失敗 2 個" in text
    assert "/media/zz-bad-a.mkv" in text
    assert "/media/zz-bad-b.mkv" in text
    for word in FORBIDDEN:
        assert word not in text


def test_job_failure_only_strips_rich_fields():
    calls = []

    def urlopen(request, timeout=20):
        calls.append(request)
        return _Body(b"{}")

    assert notify.notify_job(
        "Show",
        ok=[],
        failed=[{"path": "/media/Old/a.mkv"}],
        detail="评分 9 剧情很长 TMDB ID 1 类型 电影 媒体库 时间 新入库 IMDB ID tt1",
        settings={"telegram_bot_token": "123:ABC", "telegram_chat_id": "42", "tmdb_api_key": "secret-key"},
        urlopen=urlopen,
    )
    assert len(calls) == 1
    assert calls[0].full_url.endswith("/sendMessage")
    assert "themoviedb.org" not in calls[0].full_url
    text = _message_text(calls[0])
    assert "已成功改名" not in text
    assert "成功 0 個" in text
    assert "失敗 1 個" in text
    assert "/media/Old/a.mkv" in text
    for word in FORBIDDEN:
        assert word not in text
    assert b"secret-key" not in calls[0].data


def test_job_over_ten_hides_names():
    calls = []

    def urlopen(request, timeout=20):
        calls.append(request)
        return _Body(b"{}")

    names = [f"/media/zz-hide-{index}.mkv" for index in range(11)]
    assert notify.notify_job(
        "Show",
        ok=[],
        failed=[{"path": path} for path in names],
        detail="评分 zz-hide-0.mkv",
        settings={"telegram_bot_token": "123:ABC", "telegram_chat_id": "42", "tmdb_api_key": ""},
        urlopen=urlopen,
    )
    assert len(_telegram(calls)) == 1
    text = _message_text(calls[0])
    assert "已成功改名" not in text
    assert "成功 0 個" in text
    assert "失敗 11 個" in text
    assert "紀錄" in text
    assert "zz-hide" not in text
    for word in FORBIDDEN:
        assert word not in text


def test_job_poster_uses_first_success_dir():
    calls = []

    def urlopen(request, timeout=20):
        calls.append(request)
        url = request.full_url
        if "api.themoviedb.org" in url:
            return _Body(b'{"poster_path":"/abc.jpg"}')
        if "image.tmdb.org" in url:
            return _Body(b"\xff\xd8\xffTMDB")
        return _Body(b"{}")

    with tempfile.TemporaryDirectory() as tmp:
        first = os.path.join(tmp, "first")
        second = os.path.join(tmp, "second")
        os.makedirs(first)
        os.makedirs(second)
        with open(os.path.join(first, "poster.jpg"), "wb") as handle:
            handle.write(b"DISKPOST")
        with open(os.path.join(second, "folder.jpg"), "wb") as handle:
            handle.write(b"OTHERPOST")
        settings = {"telegram_bot_token": "123:ABC", "telegram_chat_id": "42", "tmdb_api_key": "secret-key"}
        ok = [
            {"new": "Movie [tmdbid=9]/a.mkv", "poster_dir": first},
            {"new": "Movie [tmdbid=9]/b.mkv", "poster_dir": second},
        ]
        assert notify.notify_job("Show", ok=ok, failed=[{"path": "/media/zz-bad.mkv"}], settings=settings, urlopen=urlopen)
        assert len(_telegram(calls)) == 1
        assert calls[0].full_url.endswith("/sendPhoto")
        assert b"DISKPOST" in calls[0].data
        assert b"OTHERPOST" not in calls[0].data
        assert "已成功改名".encode() in calls[0].data
        text = _message_text(calls[0])
        assert "成功 2 個" in text
        assert "失敗 1 個" in text
        assert "/media/zz-bad.mkv" in text
        assert all("themoviedb.org" not in item.full_url for item in calls)


def test_engine_multi_file_one_telegram():
    calls = []
    original = _use_urlopen(calls)
    saved_env = os.environ.pop("TMDB_API_KEY", None)
    real_qbit = engine.Qbit
    real_execute = engine.execute_plans
    engine.Qbit = _FakeQbit
    engine.execute_plans = lambda *args, **kwargs: None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _arm(tmp)
            engine.Engine()._process(_folder_batch())
            _drain_telegram()
            sent = _telegram(calls)
            assert len(sent) == 1
            text = _message_text(sent[0])
            assert "已成功改名" in text
            assert "成功 3 個" in text
            assert "失敗 0 個" in text
            oks = [row for row in db.list_activity() if row["status"] == "ok"]
            assert len(oks) == 3
            assert {row["old_path"] for row in oks} == {"Old/a.mkv", "Old/b.mkv", "Old/c.mkv"}
    finally:
        engine.Qbit = real_qbit
        engine.execute_plans = real_execute
        _restore_urlopen(original)
        if saved_env is not None:
            os.environ["TMDB_API_KEY"] = saved_env


def test_engine_failure_one_telegram():
    calls = []
    original = _use_urlopen(calls)
    saved_env = os.environ.pop("TMDB_API_KEY", None)
    real_qbit = engine.Qbit
    real_execute = engine.execute_plans

    def boom(*_args, **_kwargs):
        raise engine.RenameFailed("qBittorrent 未完成改名 评分 9 剧情很长 TMDB ID 1 新入库")

    engine.Qbit = _FakeQbit
    engine.execute_plans = boom
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _arm(tmp)
            engine.Engine()._process(_folder_batch())
            _drain_telegram()
            sent = _telegram(calls)
            assert len(sent) == 1
            assert sent[0].full_url.endswith("/sendMessage")
            text = _message_text(sent[0])
            assert "已成功改名" not in text
            assert "成功 0 個" in text
            assert "失敗 3 個" in text
            assert "a.mkv" in text and "b.mkv" in text and "c.mkv" in text
            for word in FORBIDDEN:
                assert word not in text
            errors = [row for row in db.list_activity() if row["status"] == "error"]
            assert len(errors) == 1
    finally:
        engine.Qbit = real_qbit
        engine.execute_plans = real_execute
        _restore_urlopen(original)
        if saved_env is not None:
            os.environ["TMDB_API_KEY"] = saved_env


def test_silent_jobs_do_not_notify():
    calls = []
    original = _use_urlopen(calls)
    saved_env = os.environ.pop("TMDB_API_KEY", None)
    real_qbit = engine.Qbit
    engine.Qbit = _FakeQbit
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _arm(tmp, enabled=False)
            engine.Engine()._process(_folder_batch())
            _drain_telegram()
            assert _telegram(calls) == []
            stored = db.get_settings()
            stored["enabled"] = True
            stored["dry_run"] = True
            db.save_settings(stored)
            engine.Engine()._process(_folder_batch())
            _drain_telegram()
            assert _telegram(calls) == []
            stored = db.get_settings()
            stored["watch_folders"] = "/115/Video/Movie"
            stored["dry_run"] = False
            db.save_settings(stored)
            added = engine.Engine().add_webhook(_event("/115/Video/Other/a.mkv", "/115/Video/Other/b.mkv"))
            assert added == 0
            _drain_telegram()
            assert _telegram(calls) == []
    finally:
        engine.Qbit = real_qbit
        _restore_urlopen(original)
        if saved_env is not None:
            os.environ["TMDB_API_KEY"] = saved_env


def test_ambiguous_paths_one_job():
    calls = []
    original = _use_urlopen(calls)
    saved_env = os.environ.pop("TMDB_API_KEY", None)
    real_qbit = engine.Qbit

    class OneFile(_FakeQbit):
        def torrents(self):
            rows = super().torrents()
            rows[0]["save_path"] = "/downloads"
            return rows

        def files(self, _infohash):
            return [{"name": "Old/a.mkv"}]

    engine.Qbit = OneFile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = engine.norm_path(tmp)
            os.makedirs(os.path.join(tmp, "NewA"))
            os.makedirs(os.path.join(tmp, "NewB"))
            open(os.path.join(tmp, "NewA", "a.mkv"), "w").close()
            open(os.path.join(tmp, "NewB", "a.mkv"), "w").close()
            _arm(tmp, maps=[{
                "cd_prefix": "/115/Video",
                "local_prefix": root,
                "qbit_prefix": "/downloads",
            }])
            old = root + "/Old/a.mkv"
            engine.Engine()._process([
                {"src": old, "dst": root + "/NewA/a.mkv", "is_dir": False, "raw_src": old, "raw_dst": root + "/NewA/a.mkv"},
                {"src": old, "dst": root + "/NewB/a.mkv", "is_dir": False, "raw_src": old, "raw_dst": root + "/NewB/a.mkv"},
            ])
            _drain_telegram()
            sent = _telegram(calls)
            assert len(sent) == 1, [ _message_text(item) for item in sent ]
            text = _message_text(sent[0])
            assert "已成功改名" not in text
            assert "失敗 1 個" in text
            assert "a.mkv" in text
            for word in FORBIDDEN:
                assert word not in text
    finally:
        engine.Qbit = real_qbit
        _restore_urlopen(original)
        if saved_env is not None:
            os.environ["TMDB_API_KEY"] = saved_env


def test_retry_one_telegram():
    calls = []
    original = _use_urlopen(calls)
    saved_env = os.environ.pop("TMDB_API_KEY", None)
    real_qbit = engine.Qbit
    real_execute = engine.execute_plans
    engine.Qbit = _FakeQbit
    engine.execute_plans = lambda *args, **kwargs: None
    plans = [
        {"old_rel": "Old/a.mkv", "new_rel": "New/a.mkv", "host_old": "/media/Show/Old/a.mkv", "host_new": "/media/Show/New/a.mkv"},
        {"old_rel": "Old/b.mkv", "new_rel": "New/b.mkv", "host_old": "/media/Show/Old/b.mkv", "host_new": "/media/Show/New/b.mkv"},
    ]
    raw = json.dumps({"infohash": "abc123def", "name": "Show", "host_save": "/media/Show", "plans": plans})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _arm(tmp)
            engine.Engine()._retry_worker(1, raw)
            _drain_telegram()
            sent = _telegram(calls)
            assert len(sent) == 1
            text = _message_text(sent[0])
            assert "已成功改名" in text
            assert "成功 2 個" in text
            assert "失敗 0 個" in text
            oks = [row for row in db.list_activity() if row["detail"] == "重試成功，已更新 qBittorrent"]
            assert len(oks) == 2
    finally:
        engine.Qbit = real_qbit
        engine.execute_plans = real_execute
        _restore_urlopen(original)
        if saved_env is not None:
            os.environ["TMDB_API_KEY"] = saved_env


if __name__ == "__main__":
    test_watch_empty_still_processes()
    test_watch_filters_outside_paths()
    test_success_caption()
    test_failure_caption()
    test_telegram_send_respects_token()
    test_no_tmdb_without_key()
    test_disk_poster_and_tmdb_fallback()
    test_secrets_stay_masked()
    test_job_text_counts_and_failure_limit()
    test_job_one_telegram_call()
    test_job_failure_only_strips_rich_fields()
    test_job_over_ten_hides_names()
    test_job_poster_uses_first_success_dir()
    test_engine_multi_file_one_telegram()
    test_engine_failure_one_telegram()
    test_silent_jobs_do_not_notify()
    test_ambiguous_paths_one_job()
    test_retry_one_telegram()
    print("test_notify ok")
