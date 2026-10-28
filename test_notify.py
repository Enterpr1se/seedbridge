"""Watch-folder filter and Telegram caption rules."""

import json
import os
import tempfile

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


def _prepare(tmp, watch):
    db.init(tmp)
    stored = db.get_settings()
    stored["maps"] = [{"cd_prefix": "/115/Video", "local_prefix": "/media", "qbit_prefix": "/downloads"}]
    stored["watch_folders"] = watch
    stored["enabled"] = False
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
    assert "成功改名" in text
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
    assert "成功改名" in body["text"]
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
        assert b"\xe6\x88\x90\xe5\x8a\x9f\xe6\x94\xb9\xe5\x90\x8d" in calls[0].data
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
    assert "成功改名" in body["text"]
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


if __name__ == "__main__":
    test_watch_empty_still_processes()
    test_watch_filters_outside_paths()
    test_success_caption()
    test_failure_caption()
    test_telegram_send_respects_token()
    test_no_tmdb_without_key()
    test_disk_poster_and_tmdb_fallback()
    test_secrets_stay_masked()
    print("test_notify ok")
