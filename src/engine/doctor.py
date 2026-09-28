"""Chequeos de `engine doctor`. Cada chequeo devuelve un Check y nunca lanza."""

import shutil
import subprocess
from dataclasses import dataclass

import anthropic
import httpx
from googleapiclient.discovery import build

from engine import db
from engine.config import ENV_FILE, get_settings

# Video público estable, solo para validar la key (videos.list = 1 unidad).
YT_PROBE_VIDEO_ID = "jNQXAC9IVRw"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False  # no bloquea: doctor sigue en verde


def check_env() -> Check:
    if not ENV_FILE.exists():
        return Check(".env", False, f"No existe {ENV_FILE} (copia .env.example)")
    missing = get_settings().missing_keys()
    if missing:
        return Check(".env", False, "Faltan: " + ", ".join(missing))
    return Check(".env", True, "Claves obligatorias presentes")


def check_db() -> Check:
    try:
        conn = db.get_conn()
        version = db.current_version(conn)
        conn.close()
    except Exception as e:
        return Check("SQLite", False, repr(e))
    ok = version == db.latest_version()
    return Check("SQLite", ok, f"{get_settings().db_path} · schema v{version}")


def check_ollama() -> Check:
    settings = get_settings()
    try:
        r = httpx.get(f"{settings.ollama_host}/api/tags", timeout=5)
        r.raise_for_status()
    except Exception as e:
        return Check("Ollama", False, f"No responde en {settings.ollama_host}: {e}")
    available = {m["name"] for m in r.json().get("models", [])}
    missing = sorted(m for m in settings.ollama_models_in_use() if m not in available)
    if missing:
        return Check("Ollama", False, "Modelos sin descargar: " + ", ".join(missing))
    return Check("Ollama", True, "Modelos: " + ", ".join(sorted(settings.ollama_models_in_use())))


def check_gpu() -> Check:
    if not shutil.which("nvidia-smi"):
        return Check("GPU", False, "nvidia-smi no encontrado")
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except Exception as e:
        return Check("GPU", False, repr(e))
    if not out:
        return Check("GPU", False, "nvidia-smi no reporta GPUs")
    name, total, used = (x.strip() for x in out.splitlines()[0].split(","))
    return Check("GPU", True, f"{name} · VRAM {used} / {total} en uso")


def check_youtube() -> Check:
    settings = get_settings()
    if not settings.youtube_api_key:
        return Check("YouTube API", False, "YOUTUBE_API_KEY vacía")
    try:
        yt = build("youtube", "v3", developerKey=settings.youtube_api_key, cache_discovery=False)
        yt.videos().list(part="id", id=YT_PROBE_VIDEO_ID).execute()
    except Exception as e:
        return Check("YouTube API", False, _short(e))
    conn = db.get_conn()
    db.record_api_usage(conn, "youtube", "videos.list", 1)
    used = db.api_units_today(conn, "youtube")
    conn.close()
    remaining = settings.yt_daily_quota - used
    return Check(
        "YouTube API",
        remaining > 0,
        f"Key válida · cuota hoy (contador local): {used}/{settings.yt_daily_quota}",
    )


def check_anthropic() -> Check:
    settings = get_settings()
    if not settings.anthropic_api_key:
        return Check("Anthropic", True, "ANTHROPIC_API_KEY vacía · el tier premium fallará", warn=True)
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        client.models.retrieve(settings.anthropic_model)  # valida key y modelo, sin tokens
    except Exception as e:
        return Check("Anthropic", False, _short(e))
    return Check("Anthropic", True, f"Key válida · modelo premium: {settings.anthropic_model}")


def _short(e: Exception, limit: int = 160) -> str:
    text = f"{type(e).__name__}: {e}"
    return text if len(text) <= limit else text[:limit] + "…"


ALL_CHECKS = [check_env, check_db, check_ollama, check_gpu, check_youtube, check_anthropic]


def run_all() -> list[Check]:
    return [fn() for fn in ALL_CHECKS]
