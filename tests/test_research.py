from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from engine import db, research
from engine.cli import app


class FakeYouTubeAPI:
    """Imita `resource.<recurso>().<método>(**kw).execute()` con datos en memoria.

    channels: {channel_id: [views | (views, duración ISO) de sus últimos uploads]}
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
                self.videos_db[vid] = (cid, *v) if isinstance(v, tuple) else (cid, v, "PT40S")
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
    rows = research.eligible_outliers(conn, min_views=0)
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
    conn, s = _run(_fixture_api(), min_ratio=1.5)
    assert s.outliers_saved == 3  # s1, s2 (ratio 2), s3
    assert len(research.eligible_outliers(conn, min_views=0)) == 2  # top filtra por OUTLIER_RATIO_MIN
    assert len(research.eligible_outliers(conn, min_views=0, min_ratio=1.5)) == 3


def test_stale_outlier_updated_when_channel_median_changes():
    api = _fixture_api()
    conn, _ = _run(api)
    # El canal A pasa a tener mediana 4000: s1 (10K vistas) baja a ratio 2.5
    for i in range(10):
        api.videos_db[f"A-u{i}"] = ("A", 4000, "PT40S")
    with conn:
        conn.execute("UPDATE channels SET fetched_at = '2000-01-01T00:00:00+00:00'")
    _run(api)
    row = conn.execute("SELECT channel_median, outlier_ratio FROM outliers WHERE video_id = 's1'").fetchone()
    assert tuple(row) == (4000, 2.5)
    assert "s1" not in [r["video_id"] for r in research.eligible_outliers(conn, min_views=0)]


def test_ratio_recomputed_for_outliers_not_seen_again():
    api = _fixture_api()
    conn, _ = _run(api)
    api.search_ids.remove("s1")  # s1 no vuelve a salir en la búsqueda
    for i in range(10):
        api.videos_db[f"A-u{i}"] = ("A", 2000, "PT40S")
    with conn:
        conn.execute("UPDATE channels SET fetched_at = '2000-01-01T00:00:00+00:00'")
    _run(api)
    row = conn.execute("SELECT channel_median, outlier_ratio FROM outliers WHERE video_id = 's1'").fetchone()
    assert tuple(row) == (2000, 5.0)


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
    result = runner.invoke(app, ["top", "--limit", "20", "--min-views", "0"], env={"COLUMNS": "250"})
    assert result.exit_code == 0
    assert "10.0×" in result.output and "youtube.com/shorts/s1" in result.output
    assert "40.0" in result.output  # score de s1 = 10 × log10(10 000)


# --- OUTLIER_MIN_VIEWS: se guarda todo, se filtra al leer ---

def test_min_views_filters_on_read_not_on_save(monkeypatch):
    conn, s = _run(_fixture_api())
    assert s.outliers_saved == 2  # s1 (10K vistas) y s3 (5K) se guardan los dos
    assert conn.execute("SELECT COUNT(*) FROM outliers").fetchone()[0] == 2

    # por defecto OUTLIER_MIN_VIEWS = 10 000 → s3 queda fuera
    assert [r["video_id"] for r in research.eligible_outliers(conn)] == ["s1"]
    assert [r["video_id"] for r in research.eligible_outliers(conn, min_views=0)] == ["s1", "s3"]

    # cambiar el umbral en .env no requiere volver a consultar la API
    monkeypatch.setenv("OUTLIER_MIN_VIEWS", "20000")
    research.get_settings.cache_clear()
    assert research.eligible_outliers(conn) == []


def test_cli_top_applies_min_views():
    _run(_fixture_api())
    out = CliRunner().invoke(app, ["top"], env={"COLUMNS": "250"}).output
    assert "shorts/s1" in out and "shorts/s3" not in out


# --- Mediana: solo Shorts si hay ≥ 5, si no todos los uploads ---

def test_channel_median_prefers_shorts():
    sample = [(100, 30)] * 6 + [(50_000, 600)] * 10  # 6 Shorts + 10 videos largos
    assert research.channel_median(sample) == (100, "shorts", 6)


def test_channel_median_falls_back_to_all_uploads():
    sample = [(100, 30)] * 4 + [(50_000, 600)] * 10  # solo 4 Shorts
    assert research.channel_median(sample) == (50_000, "all", 14)


def test_channel_median_too_few_videos():
    assert research.channel_median([(100, 30)] * 4) == (None, None, 4)


def test_research_uses_shorts_median_end_to_end():
    api = FakeYouTubeAPI(
        channels={
            "S": [(200, "PT30S")] * 6 + [(100_000, "PT12M")] * 10,  # mediana Shorts = 200
            "L": [(200, "PT30S")] * 2 + [(1_000, "PT12M")] * 10,  # pocos Shorts → todos = 1000
        },
        shorts=[("x1", "S", 20_000, "PT40S"), ("x2", "L", 20_000, "PT40S")],
    )
    conn, s = _run(api)
    got = {r["channel_id"]: (r["median_views"], r["median_source"], r["sample_size"])
           for r in conn.execute("SELECT * FROM channels")}
    assert got == {"S": (200, "shorts", 6), "L": (1_000, "all", 12)}
    ratios = {r["video_id"]: r["outlier_ratio"] for r in conn.execute("SELECT * FROM outliers")}
    assert ratios == {"x1": 100.0, "x2": 20.0}


def test_migration_3_invalidates_old_channel_cache(tmp_path):
    conn = db.connect(tmp_path / "old.db")
    db.current_version(conn)
    for v, sql in db.MIGRATIONS[:2]:
        conn.executescript(f"BEGIN; {sql}; INSERT INTO schema_version VALUES ({v}); COMMIT;")
    with conn:
        conn.execute("INSERT INTO channels (channel_id, median_views, sample_size, fetched_at) "
                     "VALUES ('A', 1000, 30, ?)", (db.now_iso(),))
    assert db.migrate(conn) == [3]
    row = conn.execute("SELECT median_source, fetched_at FROM channels").fetchone()
    assert row["median_source"] is None and row["fetched_at"].startswith("1970")


# --- score = ratio × log10(vistas) ---

def _insert_outlier(conn, vid, views, ratio):
    with conn:
        conn.execute("INSERT OR IGNORE INTO channels (channel_id, median_views, sample_size, fetched_at) "
                     "VALUES ('Z', 1, 5, ?)",
                     (db.now_iso(),))
    research.save_outlier(
        conn,
        research.Video(vid, "Z", "", vid, "", "", 30, views, None, None),
        "n", views / ratio, ratio,
    )


def test_score_ordering_vs_ratio():
    conn = db.get_conn()
    _insert_outlier(conn, "tiny", 100, 50.0)  # score 50 × 2 = 100
    _insert_outlier(conn, "big", 10_000_000, 15.0)  # score 15 × 7 = 105
    _insert_outlier(conn, "mid", 100_000, 20.0)  # score 20 × 5 = 100 (empata con tiny)

    by_score = research.eligible_outliers(conn, min_views=0)
    assert by_score[0]["video_id"] == "big"
    assert by_score[0]["score"] == pytest.approx(105.0)
    by_ratio = research.eligible_outliers(conn, min_views=0, sort="ratio")
    assert [r["video_id"] for r in by_ratio] == ["tiny", "mid", "big"]


def test_cli_top_sort_option():
    conn = db.get_conn()
    _insert_outlier(conn, "tiny", 100, 50.0)
    _insert_outlier(conn, "big", 10_000_000, 15.0)
    runner = CliRunner()
    env = {"COLUMNS": "250"}
    out = runner.invoke(app, ["top", "--min-views", "0"], env=env).output
    assert out.index("shorts/big") < out.index("shorts/tiny")
    out = runner.invoke(app, ["top", "--min-views", "0", "--sort", "ratio"], env=env).output
    assert out.index("shorts/tiny") < out.index("shorts/big")
    assert runner.invoke(app, ["top", "--sort", "foo"]).exit_code != 0
