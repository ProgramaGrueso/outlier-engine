"""Fase 1 — Research (sin LLM).

Busca Shorts recientes de un nicho y guarda los que rinden muy por encima de la
mediana de vistas de su canal: `outlier_ratio = vistas_video / mediana_canal`.
"""

import re
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Literal

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from engine import db
from engine.config import get_settings

# Coste en unidades de cuota por llamada (YouTube Data API v3).
YT_COSTS = {"search.list": 100, "videos.list": 1, "channels.list": 1, "playlistItems.list": 1}
BATCH = 50  # máximo de ids por llamada en videos.list / channels.list

SearchOrder = Literal["viewCount", "relevance", "date"]


class QuotaExceededError(RuntimeError):
    pass


class YouTube:
    """Envoltorio de la API: registra cada llamada en `api_usage` y respeta la cuota diaria."""

    def __init__(self, conn: sqlite3.Connection, resource: Any | None = None):
        settings = get_settings()
        self.conn = conn
        self.quota = settings.yt_daily_quota
        self.resource = resource or build(
            "youtube", "v3", developerKey=settings.youtube_api_key, cache_discovery=False
        )
        self.units_used = 0

    def call(self, endpoint: str, **params: Any) -> dict:
        cost = YT_COSTS[endpoint]
        used = db.api_units_today(self.conn, "youtube")
        if used + cost > self.quota:
            raise QuotaExceededError(
                f"{endpoint} costaría {cost} unidades y hoy van {used}/{self.quota}"
            )
        resource_name, method = endpoint.split(".")
        request = getattr(getattr(self.resource, resource_name)(), method)(**params)
        try:
            return request.execute()
        finally:
            # YouTube cobra la cuota aunque la petición falle.
            db.record_api_usage(self.conn, "youtube", endpoint, cost)
            self.units_used += cost


@dataclass
class Video:
    video_id: str
    channel_id: str
    channel_title: str
    title: str
    description: str
    published_at: str
    duration_s: int
    views: int
    likes: int | None
    comments: int | None


@dataclass
class ResearchSummary:
    niche: str
    videos_scanned: int
    shorts: int
    channels_fetched: int
    channels_cached: int
    channels_skipped: int
    outliers_saved: int
    units_used: int


_DURATION_RE = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(iso: str) -> int:
    """ISO 8601 (PT1M5S) → segundos."""
    m = _DURATION_RE.match(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _chunks(items: list[str], size: int = BATCH) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _int(value: Any) -> int | None:
    return int(value) if value is not None else None


def search_video_ids(
    yt: YouTube,
    niche: str,
    days: int,
    pages: int,
    order: SearchOrder = "viewCount",
    lang: str | None = None,
    region: str | None = None,
) -> list[str]:
    published_after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params: dict[str, Any] = {
        "part": "id",
        "q": niche,
        "type": "video",
        "videoDuration": "short",  # < 4 min; el filtro fino es SHORT_MAX_SECONDS
        "publishedAfter": published_after,
        "order": order,
        "maxResults": BATCH,
    }
    if lang:
        params["relevanceLanguage"] = lang
    if region:
        params["regionCode"] = region
    ids: list[str] = []
    page_token = None
    for _ in range(pages):
        resp = yt.call("search.list", **params, **({"pageToken": page_token} if page_token else {}))
        ids += [it["id"]["videoId"] for it in resp.get("items", []) if it["id"].get("videoId")]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return list(dict.fromkeys(ids))


def fetch_videos(yt: YouTube, ids: list[str]) -> list[Video]:
    videos = []
    for chunk in _chunks(ids):
        resp = yt.call("videos.list", part="snippet,statistics,contentDetails", id=",".join(chunk))
        for it in resp.get("items", []):
            sn, st = it["snippet"], it.get("statistics", {})
            if sn.get("liveBroadcastContent", "none") != "none" or "viewCount" not in st:
                continue
            videos.append(
                Video(
                    video_id=it["id"],
                    channel_id=sn["channelId"],
                    channel_title=sn.get("channelTitle", ""),
                    title=sn.get("title", ""),
                    description=sn.get("description", ""),
                    published_at=sn.get("publishedAt", ""),
                    duration_s=parse_duration(it.get("contentDetails", {}).get("duration", "")),
                    views=int(st["viewCount"]),
                    likes=_int(st.get("likeCount")),
                    comments=_int(st.get("commentCount")),
                )
            )
    return videos


def _fresh_channels(conn: sqlite3.Connection, channel_ids: list[str]) -> dict[str, float | None]:
    """Canales consultados hace menos de CHANNEL_CACHE_DAYS → {channel_id: mediana}."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=get_settings().channel_cache_days)).isoformat(
        timespec="seconds"
    )
    fresh = {}
    for chunk in _chunks(channel_ids, 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT channel_id, median_views FROM channels WHERE channel_id IN ({marks}) AND fetched_at >= ?",
            (*chunk, cutoff),
        )
        fresh.update({r["channel_id"]: r["median_views"] for r in rows})
    return fresh


def refresh_channels(yt: YouTube, conn: sqlite3.Connection, channel_ids: list[str]) -> dict[str, float | None]:
    """Consulta la API para calcular la mediana de vistas de cada canal y la guarda en caché."""
    settings = get_settings()
    info: dict[str, dict] = {}
    for chunk in _chunks(channel_ids):
        resp = yt.call("channels.list", part="snippet,statistics,contentDetails", id=",".join(chunk))
        for it in resp.get("items", []):
            info[it["id"]] = {
                "title": it["snippet"].get("title", ""),
                "subscribers": _int(it.get("statistics", {}).get("subscriberCount")),
                "uploads": it["contentDetails"]["relatedPlaylists"]["uploads"],
            }

    # Últimos N uploads de cada canal.
    samples: dict[str, list[str]] = {}
    for cid, ch in info.items():
        try:
            resp = yt.call(
                "playlistItems.list",
                part="contentDetails",
                playlistId=ch["uploads"],
                maxResults=min(settings.channel_sample_size, BATCH),
            )
        except HttpError:
            samples[cid] = []  # playlist vacía o privada (404)
            continue
        samples[cid] = [it["contentDetails"]["videoId"] for it in resp.get("items", [])]

    # Vistas de todas las muestras en lotes de 50, mezclando canales.
    all_ids = [vid for ids in samples.values() for vid in ids]
    views: dict[str, int] = {}
    for chunk in _chunks(all_ids):
        resp = yt.call("videos.list", part="statistics", id=",".join(chunk))
        for it in resp.get("items", []):
            if "viewCount" in it.get("statistics", {}):
                views[it["id"]] = int(it["statistics"]["viewCount"])

    medians: dict[str, float | None] = {}
    now = db.now_iso()
    with conn:
        for cid in channel_ids:
            ch = info.get(cid)
            sample = [views[v] for v in samples.get(cid, []) if v in views]
            median = statistics.median(sample) if len(sample) >= settings.channel_min_videos else None
            medians[cid] = median
            conn.execute(
                """INSERT INTO channels
                   (channel_id, title, subscribers, uploads_playlist, median_views, sample_size, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (channel_id) DO UPDATE SET
                     title = excluded.title, subscribers = excluded.subscribers,
                     uploads_playlist = excluded.uploads_playlist, median_views = excluded.median_views,
                     sample_size = excluded.sample_size, fetched_at = excluded.fetched_at""",
                (
                    cid,
                    ch["title"] if ch else None,
                    ch["subscribers"] if ch else None,
                    ch["uploads"] if ch else None,
                    median,
                    len(sample),
                    now,
                ),
            )
    return medians


def save_outlier(conn: sqlite3.Connection, v: Video, niche: str, median: float, ratio: float) -> None:
    now = db.now_iso()
    with conn:
        conn.execute(
            """INSERT INTO outliers
               (video_id, channel_id, niche, title, description, published_at, duration_s,
                views, likes, comments, channel_median, outlier_ratio, first_seen, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (video_id) DO UPDATE SET
                 views = excluded.views, likes = excluded.likes, comments = excluded.comments,
                 channel_median = excluded.channel_median, outlier_ratio = excluded.outlier_ratio,
                 updated_at = excluded.updated_at""",
            (
                v.video_id, v.channel_id, niche, v.title, v.description, v.published_at,
                v.duration_s, v.views, v.likes, v.comments, median, ratio, now, now,
            ),
        )


def research(
    niche: str,
    *,
    days: int = 30,
    pages: int = 2,
    order: SearchOrder = "viewCount",
    lang: str | None = None,
    region: str | None = None,
    min_ratio: float | None = None,
    conn: sqlite3.Connection | None = None,
    yt: YouTube | None = None,
) -> ResearchSummary:
    settings = get_settings()
    min_ratio = settings.outlier_ratio_min if min_ratio is None else min_ratio
    conn = conn or db.get_conn()
    yt = yt or YouTube(conn)

    ids = search_video_ids(yt, niche, days, pages, order, lang, region)
    videos = fetch_videos(yt, ids)
    shorts = [v for v in videos if 0 < v.duration_s <= settings.short_max_seconds]

    channel_ids = list(dict.fromkeys(v.channel_id for v in shorts))
    medians = _fresh_channels(conn, channel_ids)
    cached = len(medians)
    to_fetch = [c for c in channel_ids if c not in medians]
    if to_fetch:
        medians.update(refresh_channels(yt, conn, to_fetch))

    saved = 0
    for v in shorts:
        median = medians.get(v.channel_id)
        if not median:
            continue
        ratio = v.views / median
        if ratio >= min_ratio:
            save_outlier(conn, v, niche, median, ratio)
            saved += 1

    summary = ResearchSummary(
        niche=niche,
        videos_scanned=len(videos),
        shorts=len(shorts),
        channels_fetched=len(to_fetch),
        channels_cached=cached,
        channels_skipped=sum(1 for c in channel_ids if not medians.get(c)),
        outliers_saved=saved,
        units_used=yt.units_used,
    )
    with conn:
        conn.execute(
            """INSERT INTO research_runs
               (ts, niche, days, pages, videos_scanned, shorts, channels_fetched,
                channels_cached, outliers_saved, units_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                db.now_iso(), niche, days, pages, summary.videos_scanned, summary.shorts,
                summary.channels_fetched, summary.channels_cached, saved, summary.units_used,
            ),
        )
    return summary


def top(conn: sqlite3.Connection, limit: int = 20, niche: str | None = None) -> list[sqlite3.Row]:
    sql = """SELECT o.*, c.title AS channel_title FROM outliers o
             LEFT JOIN channels c USING (channel_id)"""
    params: tuple = ()
    if niche:
        sql += " WHERE o.niche = ?"
        params = (niche,)
    sql += " ORDER BY o.outlier_ratio DESC LIMIT ?"
    return conn.execute(sql, (*params, limit)).fetchall()


def short_url(video_id: str) -> str:
    return f"https://youtube.com/shorts/{video_id}"
