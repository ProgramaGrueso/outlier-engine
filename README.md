# Outlier Engine

Pipeline de formatos virales para video corto: encuentra Shorts outlier, extrae su
formato, genera guiones de frutinovelas y los manda a ComfyUI. Ver `CLAUDE.md` para el
diseño completo por fases.

## Estado

| Fase | Estado |
|---|---|
| 0 — Setup | ✅ |
| 1 — Research | ✅ |
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


## Fase 1 — Research

Busca Shorts recientes de un nicho y guarda los que rinden muy por encima de su canal:
`outlier_ratio = vistas_video / mediana_canal`, y los ordena por
`score = ratio × log10(vistas)` para premiar alcance real y no solo ratio.

```bash
uv run engine research "frutas con inteligencia artificial" --days 30 --lang es
uv run engine top --limit 20
```

Opciones de `research`:

| Opción | Por defecto | Qué hace |
|---|---|---|
| `--days` | 30 | solo Shorts publicados en los últimos N días |
| `--pages` | 2 | páginas de `search.list` (50 resultados y **100 unidades** cada una) |
| `--order` | `viewCount` | `viewCount`, `relevance` o `date` |
| `--lang` / `--region` | — | `relevanceLanguage` (ej. `es`) / `regionCode` (ej. `MX`) |
| `--min-ratio` | `OUTLIER_RATIO_MIN` (5) | umbral para guardar un outlier |

Opciones de `top`:

| Opción | Por defecto | Qué hace |
|---|---|---|
| `--limit` | 20 | cuántos mostrar |
| `--sort` | `score` | `score` (ratio × log10 vistas) o `ratio` |
| `--min-views` | `OUTLIER_MIN_VIEWS` (10 000) | vistas mínimas |
| `--min-ratio` | `OUTLIER_RATIO_MIN` (5) | ratio mínimo |
| `--niche` | — | texto exacto usado en `research` |

`research` **guarda todos** los outliers con ratio ≥ umbral, tengan las vistas que tengan.
Los filtros de vistas y ratio se aplican al leer (`engine top` y la entrada de la Fase 2,
`research.eligible_outliers()`), así que se pueden cambiar sin volver a gastar cuota.

### Cómo funciona
1. `search.list` con `videoDuration=short` (< 4 min) y `publishedAfter`.
2. `videos.list` (lotes de 50) para vistas y duración; se quedan los de
   ≤ `SHORT_MAX_SECONDS` (180 s) y se descartan directos.
3. Por cada canal nuevo: `channels.list` → playlist de uploads → `playlistItems.list`
   (últimos `CHANNEL_SAMPLE_SIZE`) → `videos.list` para vistas y duración.
   La mediana usa **solo los Shorts** del canal si hay al menos `CHANNEL_MIN_VIDEOS` (5);
   si no, todos los uploads (columna `Base` en `top`). Con menos de 5 videos en total, el
   canal queda sin mediana y se ignora.
4. Los canales se cachean en la tabla `channels` y no se vuelven a consultar durante
   `CHANNEL_CACHE_DAYS` (7) días.
5. Outliers con ratio ≥ umbral → tabla `outliers`. Si un outlier ya guardado vuelve a
   aparecer, se actualizan vistas y ratio aunque ya no pase el umbral; y al refrescar un
   canal se recalcula el ratio de todos sus outliers con la mediana nueva.
   Cada corrida queda en `research_runs`.

### Cuota
Una corrida con `--pages 2` y ~60 canales nuevos cuesta ≈ 290 unidades (200 de búsqueda +
~1,5 por canal). Si la próxima llamada superaría `YT_DAILY_QUOTA`, se aborta antes de hacerla.
Consultar el gasto del día: `engine doctor`.

## Tests
```bash
uv run pytest
```
Todas las APIs externas están mockeadas; los tests no gastan cuota ni tokens.
