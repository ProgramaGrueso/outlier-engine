"""Cliente LLM de dos niveles.

- cheap:   Ollama local. El modelo depende del rol (classify, write, critic, scenes).
- premium: API de Anthropic. Solo donde CLAUDE.md lo indique (juez de la Fase 3).

Cada llamada, exitosa o no, queda registrada en la tabla `llm_calls`.
"""

import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

import anthropic
import httpx

from engine import db
from engine.config import PROMPTS_DIR, ModelRole, get_settings

Tier = Literal["cheap", "premium"]

OLLAMA_TIMEOUT = httpx.Timeout(10.0, read=600.0)


class MissingAPIKeyError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    tier: Tier
    model: str
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int


def complete(
    prompt: str,
    *,
    task: str,
    tier: Tier = "cheap",
    role: ModelRole = "classify",
    system: str | None = None,
    json_mode: bool = False,
    temperature: float | None = None,
    max_tokens: int = 4096,
    conn: sqlite3.Connection | None = None,
) -> LLMResult:
    """Ejecuta una llamada LLM y la registra en `llm_calls`.

    `task` es la etiqueta libre que se guarda (ej. "analyze_format").
    `role` elige el modelo de Ollama en el nivel cheap; se ignora en premium.
    `json_mode` fuerza salida JSON en Ollama; la validación pydantic la hace quien llama.
    """
    settings = get_settings()
    if tier == "premium" and not settings.anthropic_api_key.strip():
        raise MissingAPIKeyError("ANTHROPIC_API_KEY vacía en .env: el tier premium no está disponible")
    model = settings.ollama_model_for(role) if tier == "cheap" else settings.anthropic_model
    own_conn = conn is None
    conn = conn or db.get_conn()
    start = time.monotonic()
    try:
        if tier == "cheap":
            text, t_in, t_out = _ollama_chat(model, prompt, system, json_mode, temperature, max_tokens)
        else:
            text, t_in, t_out = _anthropic_message(model, prompt, system, temperature, max_tokens)
    except Exception as e:
        _log(conn, tier, model, task, None, None, _elapsed_ms(start), ok=False, error=repr(e))
        if own_conn:
            conn.close()
        raise
    result = LLMResult(text, tier, model, t_in, t_out, _elapsed_ms(start))
    _log(conn, tier, model, task, t_in, t_out, result.latency_ms, ok=True)
    if own_conn:
        conn.close()
    return result


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _ollama_chat(
    model: str,
    prompt: str,
    system: str | None,
    json_mode: bool,
    temperature: float | None,
    max_tokens: int,
) -> tuple[str, int | None, int | None]:
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": prompt})
    options: dict = {"num_predict": max_tokens}
    if temperature is not None:
        options["temperature"] = temperature
    payload: dict = {"model": model, "messages": messages, "stream": False, "options": options}
    if json_mode:
        payload["format"] = "json"
    r = httpx.post(f"{get_settings().ollama_host}/api/chat", json=payload, timeout=OLLAMA_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data["message"]["content"], data.get("prompt_eval_count"), data.get("eval_count")


def _anthropic_message(
    model: str,
    prompt: str,
    system: str | None,
    temperature: float | None,
    max_tokens: int,
) -> tuple[str, int | None, int | None]:
    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature
    msg = client.messages.create(**kwargs)
    text = "".join(b.text for b in msg.content if b.type == "text")
    return text, msg.usage.input_tokens, msg.usage.output_tokens


def _log(
    conn: sqlite3.Connection,
    tier: Tier,
    model: str,
    task: str,
    tokens_in: int | None,
    tokens_out: int | None,
    latency_ms: int,
    *,
    ok: bool,
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """INSERT INTO llm_calls
               (ts, tier, model, task, tokens_in, tokens_out, latency_ms, ok, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (db.now_iso(), tier, model, task, tokens_in, tokens_out, latency_ms, int(ok), error),
        )


def load_prompt(name: str, **variables: str) -> str:
    """Lee `prompts/<name>.md` y sustituye `{{variable}}`.

    Se usa `{{...}}` y no `str.format` para que las plantillas puedan contener JSON literal.
    """
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def loaded_ollama_models() -> list[str]:
    """Modelos de Ollama cargados ahora mismo en memoria (GET /api/ps)."""
    r = httpx.get(f"{get_settings().ollama_host}/api/ps", timeout=10)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", [])]


def unload_ollama(models: list[str] | None = None) -> list[str]:
    """Descarga modelos de Ollama de la VRAM (keep_alive: 0).

    Sin argumentos descarga todos los que estén cargados. Pensado para liberar
    la GPU antes de mandar trabajo a ComfyUI (Fase 4). Devuelve los descargados.
    """
    host = get_settings().ollama_host
    targets = models if models is not None else loaded_ollama_models()
    for model in targets:
        r = httpx.post(
            f"{host}/api/generate", json={"model": model, "keep_alive": 0}, timeout=60
        )
        r.raise_for_status()
    return targets
