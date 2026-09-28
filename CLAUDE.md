# Proyecto: Outlier Engine — pipeline de formatos virales para video corto

## Contexto
Soy JF. Produzco videos cortos generados con IA (serie actual: "frutinovelas", frutas
antropomorfizadas con humor negro, 40–60 s). Quiero un pipeline que:
1. Encuentre videos cortos que rinden muy por encima del promedio de su canal (outliers).
2. Extraiga su FORMATO (estructura, hook, ritmo), nunca su contenido.
3. Genere guiones propios con ese formato.
4. Los convierta en prompts para mi ComfyUI local (MiniMax H3, formato REF2VA).
5. Mida qué funciona para retroalimentar el ciclo.

Hardware: Ryzen 7 5700X3D, RTX 4070 Ti Super 16 GB, 64 GB RAM, CachyOS.
Ya tengo: Ollama, ComfyUI, Hermes (agente local).

## Reglas duras
- NUNCA descargar videos ajenos para republicarlos. Solo metadatos, título, descripción
  y el audio de los primeros ~10 s para transcribir. El audio se borra tras transcribir.
- Empezar SOLO con YouTube Shorts (API oficial). TikTok/Instagram quedan fuera del MVP.
- Construir una fase por sesión. No avanzar sin que la fase anterior pase sus criterios.
- Nada de secretos en el código: todo en `.env` (incluir `.env.example`).

## Stack
- Python 3.11+, `uv` para dependencias, CLI con `typer`.
- SQLite (`data/outliers.db`) como única base de datos.
- `google-api-python-client` (YouTube Data API v3), `yt-dlp` (solo audio parcial),
  `faster-whisper` (GPU) para transcripción.
- Cliente LLM propio en `llm.py` con dos niveles:
  - `cheap`: Ollama local (modelo configurable en `.env`, ej. qwen3 / llama).
  - `premium`: API de Anthropic (Claude), solo donde se indique abajo.
  - Registrar en la tabla `llm_calls` cada llamada: nivel, modelo, tokens in/out, tarea.

## Estructura
```
outlier-engine/
  src/engine/
    cli.py  config.py  db.py  llm.py
    research.py   # Fase 1
    analyze.py    # Fase 2
    ideate.py     # Fase 3
    produce.py    # Fase 4
    metrics.py    # Fase 5
  prompts/        # plantillas .md de cada tarea LLM, editables sin tocar código
  workflows/      # workflow de ComfyUI exportado en formato API (.json)
  data/  tests/  .env.example  README.md
```

## Fase 0 — Setup
- Esqueleto del repo, `db.py` con migraciones simples, `llm.py` con ambos niveles.
- Comando `engine doctor`: verifica .env, Ollama activo, GPU visible, cuota de API.
- Criterio: `engine doctor` en verde.

## Fase 1 — Research (sin LLM)
- `engine research "<nicho>" --days 30`: busca Shorts recientes del nicho.
- Para cada canal encontrado: mediana de vistas de sus últimos 30 videos.
- `outlier_ratio = vistas_video / mediana_canal`. Guardar si ratio >= 5 (configurable).
- Cuidar la cuota: search.list cuesta 100 unidades (10 000/día). Cachear canales en SQLite
  y no volver a consultarlos en 7 días.
- Criterio: `engine top --limit 20` muestra tabla con link, vistas, mediana, ratio.

## Fase 2 — Análisis (LLM cheap)
- Para cada outlier: transcribir los primeros 10 s con faster-whisper, borrar audio.
- LLM cheap devuelve JSON validado (pydantic):
  `hook_2s`, `formato` (1 frase, sin tema), `tema`, `estructura` (beats),
  `reskin_score` 1–10 (qué tan fácil se aplica a frutinovelas) y `por_que_funciona`.
- Si el JSON falla, reintentar 2 veces con el cheap; nunca escalar a premium.
- Criterio: `engine formats` agrupa formatos similares y los ordena por ratio × reskin_score.

## Fase 3 — Ideación (generador–crítico)
- `engine ideate --format <id> --n 10`:
  1. Guionista (cheap) genera 10 guiones de 40–60 s con el formato elegido y personajes
     del universo frutinovelas (plantilla en `prompts/guionista.md`).
  2. Crítico (cheap) puntúa cada uno en hook, claridad, remate y rewatch; devuelve feedback.
  3. Los rechazados se reescriben una vez con el feedback.
  4. Solo el top 3 va al juez `premium` (Claude), que elige 1 y propone mejoras concretas.
- Guardar todas las versiones y puntajes en SQLite.
- Criterio: 1 lote completo con como máximo 1 llamada premium, visible en `llm_calls`.

## Fase 4 — Producción
- Convertir el guion elegido en escenas → prompts REF2VA (bloques `subject_definitions`,
  `retention_analysis`, `detailed_description`), con plantilla en `prompts/ref2va.md` (cheap).
- Enviar a ComfyUI vía `POST /prompt` usando `workflows/*.json`; esperar resultados por
  websocket y guardar los clips en `data/renders/<guion_id>/`.
- El montaje final y la publicación son MANUALES en el MVP.
- Criterio: un guion produce sus clips sin intervención.

## Fase 5 — Métricas
- Registrar manualmente el link del video publicado: `engine publish-log <guion_id> <url>`.
- Leer vistas a 24 h, 72 h y 7 días (YouTube Data API; retención con YouTube Analytics
  API si hay OAuth del canal propio).
- `engine report`: qué formatos y hooks rinden mejor en MI canal. Ese reporte alimenta la
  plantilla del crítico en la Fase 3.

## Forma de trabajo del agente
- Antes de codificar cada fase: plan breve y preguntas si algo es ambiguo.
- Tests mínimos por fase (mocks para APIs externas).
- Al terminar: actualizar README con cómo ejecutar esa fase.
