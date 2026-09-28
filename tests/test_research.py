from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from engine import db, research
from engine.cli import app


class FakeYouTubeAPI:
    """Imita `resource.<recurso>().<método>(**kw).execute()` con datos en memoria.

    channels: {channel_id: [views de sus últimos uploads]}
    shorts:   [(video_id, channel_id, views, duración ISO)] que devuelve search.list
    """

    def __init__(self, channels, shorts, missing_playlists=()):
        self.calls = []
        self.videos_db = {}
        self.uploads = {}
        self.missing_playlists = set(missing_playlists)
        for cid, views in channels.items():
            ids = [f"{cid}-u{i}" for i in range(len(views))]
            self.uploads[f"UU{cid}"] = ids
            for vid, v in zip(ids, views):
                self.videos_db[vid] = (cid, v, "PT40S")
        self.search_ids = []
        for vid, cid, v, dur in shorts:
            self.videos_db[vid] = (cid, v, dur)
            self.search_ids.append(vid)

    def _req(self, endpoint, fn):
        def list_(**kw):
            self.calls.append((endpoint, kw))
            return SimpleNamespace(execute=lambda: fn(**kw))

        return lambda: SimpleNamespace(list=list_)

    def __getattr__(self, name):
        handlers = {
            "search": self._search,
            "videos": self._videos,
            "channels": self._channels,
            "playlistItems": self._playlist_items,
        }
        return self._req(f"{name}.list", handlers[name])

    def _search(self, **kw):
        return {"items": [{"id": {"videoId": v}} for v in self.search_ids]}

    def _videos(self, part, id):
        items = []
        for vid in id.split(","):
            cid, views, dur = self.videos_db[vid]
            items.append({
                "id": vid,
                "snippet": {"channelId": cid, "channelTitle": f"Canal {cid}", "title": f"T {vid}",
                            "description": "", "publishedAt": "2026-09-20T00:00:00Z",
                            "liveBroadcastContent": "none"},
                "statistics": {"viewCount": str(views), "likeCount": "10"},
                "contentDetails": {"duration": dur},
            })
        return {"items": items}

    def _channels(self, part, id):
        return {"items": [
            {"id": cid, "snippet": {"title": f"Canal {cid}"}, "statistics": {"subscriberCount": "1000"},
             "contentDetails": {"relatedPlaylists": {"uploads": f"UU{cid}"}}}
            for cid in id.split(",")
        ]}

    def _playlist_items(self, part, playlistId, maxResults):
        if playlistId in self.missing_playlists:
            from googleapiclient.errors import HttpError
            raise HttpError(SimpleNamespace(status=404, reason="not found"), b"")
        ids = self.uploads.get(playlistId, [])[:maxResults]
        return {"items": [{"contentDetails": {"videoId": v}} for v in ids]}

    def count(self, endpoint):
        return sum(1 for e, _ in self.calls if e == endpoint)


def _fixture_api(**kw):
    return FakeYouTubeAPI(
        channels={
            "A": [1000] * 10,  # mediana 1000
            "B": [500] * 10,  # mediana 500
            "C": [100] * 3,  # pocos videos → sin mediana
        },
        shorts=[
            ("s1", "A", 10_000, "PT45S"),  # ratio 10 → outlier
            ("s2", "A", 2_000, "PT50S"),  # ratio 2 → no
            ("s3", "B", 5_000, "PT1M30S"),  # ratio 10, 90 s → outlier
            ("s4", "B", 90_000, "PT3M30S"),  # 210 s → no es Short
            ("s5", "C", 1_000_000, "PT30S"),  # canal sin mediana fiable
        ],
        **kw,
    )


def _run(api, **kw):
    conn = db.get_conn()
    yt = research.YouTube(conn, resource=api)
    return conn, research.research("frutas ia", conn=conn, yt=yt, pages=1, **kw)


@pytest.mark.parametrize("iso,seconds", [("PT45S", 45), ("PT1M", 60), ("PT1M30S", 90), ("PT1H2M3S", 3723), ("P0D", 0), ("", 0)])
def test_parse_duration(iso, seconds):
    assert research.parse_duration(iso) == seconds


def test_research_finds_outliers():
    api = _fixture_api()
    conn, s = _run(api)

    assert (s.videos_scanned, s.shorts, s.channels_fetched, s.channels_skipped, s.outliers_saved) == (5, 4, 3, 1, 2)
    rows = research.top(conn)
    assert [(r["video_id"], r["outlier_ratio"], r["channel_median"]) for r in rows] == [
        ("s1", 10.0, 1000), ("s3", 10.0, 500),
    ]
    assert rows[0]["channel_title"] == "Canal A"


def test_quota_is_recorded():
    api = _fixture_api()
    conn, s = _run(api)
    # 1 search (100) + 1 videos + 1 channels + 3 playlistItems + 1 videos (23 ids de muestra) = 106
    assert s.units_used == 106
    assert db.api_units_today(conn) == 106
    assert conn.execute("SELECT units_used FROM research_runs").fetchone()[0] == 106


def test_channels_cached_for_7_days():
    api = _fixture_api()
    conn, _ = _run(api)
    _, s2 = _run(api)
    assert (s2.channels_fetched, s2.channels_cached) == (0, 3)
    assert api.count("channels.list") == 1 and api.count("playlistItems.list") == 3

    with conn:
        conn.execute("UPDATE channels SET fetched_at = '2000-01-01T00:00:00+00:00' WHERE channel_id = 'A'")
    _, s3 = _run(api)
    assert (s3.channels_fetched, s3.channels_cached) == (1, 2)


def test_rerun_updates_instead_of_duplicating():
    api = _fixture_api()
    conn, _ = _run(api)
    api.videos_db["s1"] = ("A", 20_000, "PT45S")
    _run(api)
    rows = conn.execute("SELECT views, outlier_ratio FROM outliers WHERE video_id = 's1'").fetchall()
    assert [tuple(r) for r in rows] == [(20_000, 20.0)]


def test_min_ratio_override():
    _, s = _run(_fixture_api(), min_ratio=1.5)
    assert s.outliers_saved == 3  # s1, s2 (ratio 2), s3


def test_missing_playlist_skips_channel():
    _, s = _run(_fixture_api(missing_playlists={"UUA"}))
    assert s.outliers_saved == 1  # solo s3


def test_quota_guard_blocks_before_calling(monkeypatch):
    monkeypatch.setenv("YT_DAILY_QUOTA", "150")
    research.get_settings.cache_clear()
    conn = db.get_conn()
    db.record_api_usage(conn, "youtube", "search.list", 100)
    api = _fixture_api()
    with pytest.raises(research.QuotaExceededError):
        research.research("x", conn=conn, yt=research.YouTube(conn, resource=api))
    assert api.calls == []


def test_cli_top(monkeypatch):
    runner = CliRunner()
    assert "Sin outliers" in runner.invoke(app, ["top"]).output
    _run(_fixture_api())
    result = runner.invoke(app, ["top", "--limit", "20"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "10.0×" in result.output and "youtube.com/shorts/s1" in result.output
