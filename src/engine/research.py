"""Fase 1 — Research (sin LLM).

Busca Shorts recientes de un nicho y guarda los que rinden muy por encima de la
mediana de vistas de su canal: `outlier_ratio = vistas_video / mediana_canal`.
"""

import re
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from engine import db
from engine.config import NICHES_FILE, get_settings

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
    language: str | None = None  # ISO de 2 letras
    category_id: str | None = None


def video_language(snippet: dict) -> str | None:
    """Idioma declarado del video: audio primero, luego el de título/descripción."""
    lang = snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage")
    return lang.lower()[:2] if lang else None


def passes_content_filters(v: Video) -> bool:
    """RESULT_LANGUAGES y EXCLUDE_CATEGORY_IDS. Sin idioma declarado se deja pasar."""
    settings = get_settings()
    langs = settings.languages()
    if langs and v.language and v.language not in langs:
        return False
    return v.category_id not in settings.excluded_categories()


@dataclass
class ResearchSummary:
    niche: str
    videos_scanned: int
    shorts: int
    filtered: int  # Shorts descartados por idioma o categoría
    channels_fetched: int
    channels_cached: int
    channels_skipped: int
    outliers_saved: int
    units_used: int


@dataclass
class Niche:
    query: str
    lang: str | None = None


def load_niches(path: Path = NICHES_FILE) -> list[Niche]:
    """Lee `niches.txt`: una consulta por línea, `consulta | idioma` (idioma opcional)."""
    niches = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        query, _, lang = (part.strip() for part in line.partition("|"))
        if query:
            niches.append(Niche(query, lang or None))
    return list({n.query: n for n in niches}.values())  # sin duplicados, conserva el orden


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
                    language=video_language(sn),
                    category_id=sn.get("categoryId"),
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

    # Vistas y duración de todas las muestras en lotes de 50, mezclando canales.
    all_ids = [vid for ids in samples.values() for vid in ids]
    stats: dict[str, tuple[int, int]] = {}  # video_id → (vistas, duración en s)
    for chunk in _chunks(all_ids):
        resp = yt.call("videos.list", part="statistics,contentDetails", id=",".join(chunk))
        for it in resp.get("items", []):
            if "viewCount" in it.get("statistics", {}):
                duration = parse_duration(it.get("contentDetails", {}).get("duration", ""))
                stats[it["id"]] = (int(it["statistics"]["viewCount"]), duration)

    medians: dict[str, float | None] = {}
    now = db.now_iso()
    with conn:
        for cid in channel_ids:
            ch = info.get(cid)
            sample = [stats[v] for v in samples.get(cid, []) if v in stats]
            median, source, size = channel_median(sample)
            medians[cid] = median
            conn.execute(
                """INSERT INTO channels
                   (channel_id, title, subscribers, uploads_playlist, median_views, median_source,
                    sample_size, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (channel_id) DO UPDATE SET
                     title = excluded.title, subscribers = excluded.subscribers,
                     uploads_playlist = excluded.uploads_playlist, median_views = excluded.median_views,
                     median_source = excluded.median_source, sample_size = excluded.sample_size,
                     fetched_at = excluded.fetched_at""",
                (
                    cid,
                    ch["title"] if ch else None,
                    ch["subscribers"] if ch else None,
                    ch["uploads"] if ch else None,
                    median,
                    source,
                    size,
                    now,
                ),
            )
    return medians


MedianSource = Literal["shorts", "all"]


def channel_median(sample: list[tuple[int, int]]) -> tuple[float | None, MedianSource | None, int]:
    """Mediana de vistas de un canal a partir de [(vistas, duración_s)].

    Usa solo los Shorts si hay al menos CHANNEL_MIN_VIDEOS (compara Short contra Short);
    si no, todos los uploads. Devuelve (mediana, fuente, tamaño de la muestra usada).
    """
    settings = get_settings()
    shorts = [v for v, d in sample if 0 < d <= settings.short_max_seconds]
    if len(shorts) >= settings.channel_min_videos:
        return statistics.median(shorts), "shorts", len(shorts)
    if len(sample) >= settings.channel_min_videos:
        return statistics.median(v for v, _ in sample), "all", len(sample)
    return None, None, len(sample)


def _existing_outliers(conn: sqlite3.Connection, video_ids: list[str]) -> set[str]:
    found: set[str] = set()
    for chunk in _chunks(video_ids, 500):
        marks = ",".join("?" * len(chunk))
        found |= {r[0] for r in conn.execute(f"SELECT video_id FROM outliers WHERE video_id IN ({marks})", chunk)}
    return found


def _recompute_ratios(conn: sqlite3.Connection, channel_ids: list[str]) -> None:
    """Tras refrescar canales, alinea el ratio de sus outliers guardados con la mediana nueva."""
    now = db.now_iso()
    with conn:
        for chunk in _chunks(channel_ids, 500):
            marks = ",".join("?" * len(chunk))
            conn.execute(
                f"""UPDATE outliers SET
                      channel_median = c.median_views,
                      outlier_ratio = outliers.views / c.median_views,
                      updated_at = ?
                    FROM channels c
                    WHERE c.channel_id = outliers.channel_id
                      AND c.median_views IS NOT NULL
                      AND outliers.channel_id IN ({marks})""",
                (now, *chunk),
            )


def save_outlier(conn: sqlite3.Connection, v: Video, niche: str, median: float, ratio: float) -> None:
    now = db.now_iso()
    with conn:
        conn.execute(
            """INSERT INTO outliers
               (video_id, channel_id, niche, title, description, published_at, duration_s,
                views, likes, comments, channel_median, outlier_ratio, language, category_id,
                first_seen, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (video_id) DO UPDATE SET
                 views = excluded.views, likes = excluded.likes, comments = excluded.comments,
                 channel_median = excluded.channel_median, outlier_ratio = excluded.outlier_ratio,
                 language = excluded.language, category_id = excluded.category_id,
                 updated_at = excluded.updated_at""",
            (
                v.video_id, v.channel_id, niche, v.title, v.description, v.published_at,
                v.duration_s, v.views, v.likes, v.comments, median, ratio, v.language,
                v.category_id, now, now,
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
    all_shorts = [v for v in videos if 0 < v.duration_s <= settings.short_max_seconds]
    # Filtrar antes de consultar canales: no gastar cuota en idiomas ajenos ni noticias.
    shorts = [v for v in all_shorts if passes_content_filters(v)]

    channel_ids = list(dict.fromkeys(v.channel_id for v in shorts))
    medians = _fresh_channels(conn, channel_ids)
    cached = len(medians)
    to_fetch = [c for c in channel_ids if c not in medians]
    if to_fetch:
        medians.update(refresh_channels(yt, conn, to_fetch))
        _recompute_ratios(conn, to_fetch)

    known = _existing_outliers(conn, [v.video_id for v in shorts])
    saved = 0
    for v in shorts:
        median = medians.get(v.channel_id)
        if not median:
            continue
        ratio = v.views / median
        if ratio >= min_ratio:
            save_outlier(conn, v, niche, median, ratio)
            saved += 1
        elif v.video_id in known:
            save_outlier(conn, v, niche, median, ratio)  # refrescar aunque ya no pase el umbral

    summary = ResearchSummary(
        niche=niche,
        videos_scanned=len(videos),
        shorts=len(all_shorts),
        filtered=len(all_shorts) - len(shorts),
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


SortKey = Literal["score", "ratio"]

# score = ratio × log10(vistas): premia outliers con alcance real, no solo ratio alto.
SCORE_SQL = "o.outlier_ratio * log10(MAX(o.views, 1))"


def eligible_outliers(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    niche: str | None = None,
    min_views: int | None = None,
    min_ratio: float | None = None,
    max_per_channel: int | None = None,
    sort: SortKey = "score",
) -> list[sqlite3.Row]:
    """Outliers que pasan los filtros de `.env`. Entrada de `engine top` y de la Fase 2.

    En la tabla se guardan todos los outliers; los umbrales (OUTLIER_MIN_VIEWS,
    OUTLIER_RATIO_MIN) y los filtros de contenido (RESULT_LANGUAGES, EXCLUDE_CATEGORY_IDS)
    se aplican aquí para poder cambiarlos sin volver a consultar la API. El ratio se
    vuelve a filtrar porque un outlier puede dejar de serlo al recalcular su canal.
    `max_per_channel` limita cuántos videos de un mismo canal aparecen (None = sin límite).
    """
    settings = get_settings()
    min_views = settings.outlier_min_views if min_views is None else min_views
    min_ratio = settings.outlier_ratio_min if min_ratio is None else min_ratio
    order = "score DESC" if sort == "score" else "outlier_ratio DESC"
    rank_by = SCORE_SQL if sort == "score" else "o.outlier_ratio"
    where = ["o.views >= ?", "o.outlier_ratio >= ?", "c.median_views IS NOT NULL"]
    params: list[Any] = [min_views, min_ratio]
    if langs := sorted(settings.languages()):
        where.append(f"(o.language IS NULL OR o.language IN ({','.join('?' * len(langs))}))")
        params += langs
    if cats := sorted(settings.excluded_categories()):
        where.append(f"(o.category_id IS NULL OR o.category_id NOT IN ({','.join('?' * len(cats))}))")
        params += cats
    if niche:
        where.append("o.niche = ?")
        params.append(niche)
    sql = f"""SELECT * FROM (
                SELECT o.*, c.title AS channel_title, c.median_source, {SCORE_SQL} AS score,
                       ROW_NUMBER() OVER (PARTITION BY o.channel_id ORDER BY {rank_by} DESC) AS rank_in_channel
                FROM outliers o JOIN channels c USING (channel_id)
                WHERE {" AND ".join(where)})"""
    if max_per_channel is not None:
        sql += " WHERE rank_in_channel <= ?"
        params.append(max_per_channel)
    sql += f" ORDER BY {order}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def short_url(video_id: str) -> str:
    return f"https://youtube.com/shorts/{video_id}"
