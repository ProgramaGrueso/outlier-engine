# Outlier Engine

Pipeline de formatos virales para video corto: encuentra Shorts outlier, extrae su
formato, genera guiones de frutinovelas y los manda a ComfyUI. Ver `CLAUDE.md` para el
diseño completo por fases.

## Estado

| Fase | Estado |
|---|---|
| 0 — Setup | ✅ |
| 1 — Research | pendiente |
| 2 — Análisis | pendiente |
| 3 — Ideación | pendiente |
| 4 — Producción | pendiente |
| 5 — Métricas | pendiente |

## Fase 0 — Setup

### Requisitos
- [`uv`](https://docs.astral.sh/uv/) (instala Python 3.12 si hace falta)
- Ollama corriendo (`ollama serve`) con el modelo por defecto: `ollama pull qwen2.5:7b`
- GPU NVIDIA con `nvidia-smi`
- API key de YouTube Data API v3
- API key de Anthropic (opcional: solo la usa el tier premium, juez de la Fase 3)

### Instalación
```bash
uv sync
cp .env.example .env    # completar YOUTUBE_API_KEY (y ANTHROPIC_API_KEY si vas a usar premium)
uv run engine doctor
```

`engine doctor` no debe mostrar ❌ (código de salida 0). ⚠️ es una advertencia que no bloquea.
Verifica:

| Chequeo | Qué hace |
|---|---|
| `.env` | existe y tiene `YOUTUBE_API_KEY` |
| SQLite | crea/migra `data/outliers.db` al último schema |
| Ollama | responde y tiene descargados todos los modelos configurados |
| GPU | `nvidia-smi` reporta la GPU |
| YouTube API | valida la key con `videos.list` (1 unidad) y muestra la cuota del día |
| Anthropic | valida key y modelo premium con `models.retrieve` (sin coste). Sin key: ⚠️; `llm.complete(tier="premium")` lanzará `MissingAPIKeyError` |

### Modelos por rol
El nivel `cheap` (Ollama) elige el modelo según el rol de la tarea. Cada rol tiene su
variable en `.env`; si está vacía se usa `OLLAMA_MODEL`:

| Variable | Rol | Fase |
|---|---|---|
| `OLLAMA_MODEL_CLASSIFY` | análisis de formato (JSON) | 2 |
| `OLLAMA_MODEL_WRITE` | guionista | 3 |
| `OLLAMA_MODEL_CRITIC` | crítico | 3 |
| `OLLAMA_MODEL_SCENES` | guion → prompts REF2VA | 4 |

El nivel `premium` usa `ANTHROPIC_MODEL` (por defecto `claude-sonnet-5`).

### Cuota de YouTube
La API no expone la cuota restante, así que cada llamada se registra en la tabla
`api_usage` y el contador se reinicia a medianoche hora del Pacífico (como la cuota real).
`YT_DAILY_QUOTA` define el límite (10 000 por defecto).

### Base de datos
`src/engine/db.py` tiene una lista ordenada de migraciones; se aplican solas al abrir la
conexión. Para cambiar el schema, agregar una migración nueva al final (nunca editar una
ya aplicada). Tablas de la Fase 0:
- `llm_calls`: cada llamada LLM (nivel, modelo, tarea, tokens in/out, latencia, error).
- `api_usage`: unidades de cuota gastadas por API y endpoint.

### Liberar VRAM
`engine.llm.unload_ollama()` descarga de la VRAM los modelos de Ollama cargados
(`keep_alive: 0`). Se usará antes de mandar trabajo a ComfyUI en la Fase 4.

### Tests
```bash
uv run pytest
```
Todas las APIs externas están mockeadas; los tests no gastan cuota ni tokens.
