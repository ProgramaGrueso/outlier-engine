"""SQLite con migraciones simples: lista ordenada de (versión, SQL)."""

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from engine.config import get_settings

# Cada fase agrega sus tablas con una migración nueva al final. Nunca editar una ya aplicada.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE llm_calls (
            id          INTEGER PRIMARY KEY,
            ts          TEXT    NOT NULL,
            tier        TEXT    NOT NULL CHECK (tier IN ('cheap', 'premium')),
            model       TEXT    NOT NULL,
            task        TEXT    NOT NULL,
            tokens_in   INTEGER,
            tokens_out  INTEGER,
            latency_ms  INTEGER,
            ok          INTEGER NOT NULL,
            error       TEXT
        );
        CREATE INDEX idx_llm_calls_ts ON llm_calls (ts);

        CREATE TABLE api_usage (
            id        INTEGER PRIMARY KEY,
            ts        TEXT    NOT NULL,
            api       TEXT    NOT NULL,
            endpoint  TEXT    NOT NULL,
            units     INTEGER NOT NULL
        );
        CREATE INDEX idx_api_usage_api_ts ON api_usage (api, ts);
        """,
    ),
    (
        2,
        """
        CREATE TABLE channels (
            channel_id        TEXT PRIMARY KEY,
            title             TEXT,
            subscribers       INTEGER,
            uploads_playlist  TEXT,
            median_views      REAL,              -- NULL si hay menos de CHANNEL_MIN_VIDEOS
            sample_size       INTEGER NOT NULL,
            fetched_at        TEXT    NOT NULL
        );

        CREATE TABLE outliers (
            video_id        TEXT PRIMARY KEY,
            channel_id      TEXT    NOT NULL REFERENCES channels (channel_id),
            niche           TEXT    NOT NULL,
            title           TEXT,
            description     TEXT,
            published_at    TEXT,
            duration_s      INTEGER,
            views           INTEGER NOT NULL,
            likes           INTEGER,
            comments        INTEGER,
            channel_median  REAL    NOT NULL,
            outlier_ratio   REAL    NOT NULL,
            first_seen      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        );
        CREATE INDEX idx_outliers_ratio ON outliers (outlier_ratio DESC);

        CREATE TABLE research_runs (
            id                INTEGER PRIMARY KEY,
            ts                TEXT    NOT NULL,
            niche             TEXT    NOT NULL,
            days              INTEGER NOT NULL,
            pages             INTEGER NOT NULL,
            videos_scanned    INTEGER NOT NULL,
            shorts            INTEGER NOT NULL,
            channels_fetched  INTEGER NOT NULL,
            channels_cached   INTEGER NOT NULL,
            outliers_saved    INTEGER NOT NULL,
            units_used        INTEGER NOT NULL
        );
        """,
    ),
    (
        3,
        """
        -- 'shorts' si la mediana sale de ≥ CHANNEL_MIN_VIDEOS Shorts, 'all' si de todos los uploads.
        ALTER TABLE channels ADD COLUMN median_source TEXT;
        -- Las medianas previas mezclaban videos largos: invalidar la caché para recalcularlas.
        UPDATE channels SET fetched_at = '1970-01-01T00:00:00+00:00';
        """,
    ),
    (
        4,
        """
        -- Idioma (ISO de 2 letras) y categoría de YouTube, para filtrar idioma y noticias.
        ALTER TABLE outliers ADD COLUMN language TEXT;
        ALTER TABLE outliers ADD COLUMN category_id TEXT;
        """,
    ),
]

# La cuota de YouTube Data API se reinicia a medianoche hora del Pacífico.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # No todas las builds de SQLite traen funciones matemáticas; se registra la nuestra.
    conn.create_function("log10", 1, math.log10, deterministic=True)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def latest_version() -> int:
    return MIGRATIONS[-1][0]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Aplica las migraciones pendientes. Devuelve las versiones aplicadas."""
    applied = []
    version = current_version(conn)
    for v, sql in MIGRATIONS:
        if v <= version:
            continue
        conn.executescript(f"BEGIN; {sql}; INSERT INTO schema_version VALUES ({v}); COMMIT;")
        applied.append(v)
    return applied


def get_conn(path: Path | None = None) -> sqlite3.Connection:
    """Conexión lista para usar, con migraciones al día."""
    conn = connect(path)
    migrate(conn)
    return conn


def record_api_usage(conn: sqlite3.Connection, api: str, endpoint: str, units: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO api_usage (ts, api, endpoint, units) VALUES (?, ?, ?, ?)",
            (now_iso(), api, endpoint, units),
        )


def quota_day_start_utc(now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(QUOTA_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def api_units_today(conn: sqlite3.Connection, api: str = "youtube") -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(units), 0) FROM api_usage WHERE api = ? AND ts >= ?",
        (api, quota_day_start_utc()),
    ).fetchone()
    return row[0]
