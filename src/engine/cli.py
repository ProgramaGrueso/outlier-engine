"""CLI `engine`."""

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from engine import db
from engine import doctor as doctor_mod
from engine import research as research_mod
from engine.config import NICHES_FILE, get_settings

app = typer.Typer(help="Outlier Engine: formatos virales → guiones → ComfyUI.", no_args_is_help=True)
console = Console()


def _compact(n: float | None) -> str:
    if n is None:
        return "—"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suffix}"
    return f"{n:.0f}"


def _per_channel_limit(value: int | None) -> int | None:
    value = get_settings().top_max_per_channel if value is None else value
    return value or None  # 0 = sin límite


@app.callback()
def main() -> None:
    """Outlier Engine."""


@app.command()
def doctor() -> None:
    """Verifica .env, base de datos, Ollama, GPU y APIs."""
    checks = doctor_mod.run_all()
    table = Table(title="engine doctor")
    table.add_column("", width=2)
    table.add_column("Chequeo")
    table.add_column("Detalle")
    for c in checks:
        icon = "❌" if not c.ok else "⚠️" if c.warn else "✅"
        table.add_row(icon, c.name, c.detail)
    console.print(table)
    if not all(c.ok for c in checks):
        raise typer.Exit(code=1)


@app.command()
def research(
    niche: Optional[str] = typer.Argument(None, help='Consulta, ej. "frutinovelas". Sin argumento: todas las de niches.txt'),
    days: int = typer.Option(30, help="Solo Shorts publicados en los últimos N días"),
    pages: int = typer.Option(2, help="Páginas de search.list por consulta (50 resultados, 100 unidades c/u)"),
    order: str = typer.Option("viewCount", help="viewCount | relevance | date"),
    lang: Optional[str] = typer.Option(None, help="relevanceLanguage, ej. es (pisa el de niches.txt)"),
    region: Optional[str] = typer.Option(None, help="regionCode, ej. MX"),
    min_ratio: Optional[float] = typer.Option(None, help="Umbral de outlier (por defecto OUTLIER_RATIO_MIN)"),
    file: Path = typer.Option(NICHES_FILE, "--file", help="Archivo de consultas"),
) -> None:
    """Busca Shorts recientes de uno o todos los nichos y guarda los outliers."""
    if order not in ("viewCount", "relevance", "date"):
        raise typer.BadParameter("order debe ser viewCount, relevance o date")
    niches = [research_mod.Niche(niche, lang)] if niche else research_mod.load_niches(file)
    if not niches:
        console.print(f"[red]No hay consultas en {file}[/red]")
        raise typer.Exit(code=1)

    conn = db.get_conn()
    if len(niches) > 1:
        remaining = get_settings().yt_daily_quota - db.api_units_today(conn)
        search_cost = len(niches) * pages * research_mod.YT_COSTS["search.list"]
        console.print(
            f"{len(niches)} consultas · búsqueda ≈ {search_cost} unidades + ~1,5 por canal nuevo "
            f"· quedan {remaining} hoy"
        )

    total_saved = 0
    for i, n in enumerate(niches):
        try:
            s = research_mod.research(
                n.query, days=days, pages=pages, order=order, lang=lang or n.lang,
                region=region, min_ratio=min_ratio, conn=conn,
            )
        except research_mod.QuotaExceededError as e:
            console.print(f"[red]Cuota agotada:[/red] {e}")
            pending = [p.query for p in niches[i:]]
            if len(niches) > 1:
                console.print("Pendientes: " + ", ".join(pending))
            raise typer.Exit(code=1)
        total_saved += s.outliers_saved
        console.print(
            f"[bold]{s.niche}[/bold]: {s.videos_scanned} videos · {s.shorts} Shorts ({s.filtered} descartados por idioma/categoría) · "
            f"canales {s.channels_fetched} nuevos / {s.channels_cached} en caché / {s.channels_skipped} sin mediana · "
            f"[green]{s.outliers_saved} outliers[/green] · {s.units_used} unidades"
        )
    console.print(f"Total: [green]{total_saved} outliers guardados[/green] · cuota hoy: {db.api_units_today(conn)}")


@app.command()
def top(
    limit: int = typer.Option(20, help="Cuántos mostrar"),
    niche: Optional[str] = typer.Option(None, help="Filtrar por nicho"),
    sort: str = typer.Option("score", help="score (ratio × log10 vistas) | ratio"),
    min_views: Optional[int] = typer.Option(None, help="Vistas mínimas (por defecto OUTLIER_MIN_VIEWS)"),
    min_ratio: Optional[float] = typer.Option(None, help="Ratio mínimo (por defecto OUTLIER_RATIO_MIN)"),
    per_channel: Optional[int] = typer.Option(None, help="Máximo por canal (por defecto TOP_MAX_PER_CHANNEL; 0 = sin límite)"),
) -> None:
    """Outliers ordenados por score = ratio × log10(vistas)."""
    if sort not in ("score", "ratio"):
        raise typer.BadParameter("sort debe ser score o ratio")
    rows = research_mod.eligible_outliers(
        db.get_conn(), limit=limit, niche=niche, min_views=min_views, min_ratio=min_ratio, sort=sort,
        max_per_channel=_per_channel_limit(per_channel),
    )
    if not rows:
        console.print("Sin outliers que pasen el filtro. Corre `engine research \"<nicho>\"` o baja --min-views.")
        return
    table = Table(title=f"Top {len(rows)} outliers · orden: {sort}")
    table.add_column("#", justify="right")
    table.add_column("Score", justify="right", style="bold green" if sort == "score" else None)
    table.add_column("Ratio", justify="right", style="bold green" if sort == "ratio" else None)
    table.add_column("Vistas", justify="right")
    table.add_column("Mediana", justify="right")
    table.add_column("Base")
    table.add_column("Canal", max_width=20, no_wrap=True)
    table.add_column("Título", max_width=40, no_wrap=True)
    table.add_column("Link", no_wrap=True)
    for i, r in enumerate(rows, 1):
        url = research_mod.short_url(r["video_id"])
        table.add_row(
            str(i),
            f"{r['score']:.1f}",
            f"{r['outlier_ratio']:.1f}×",
            _compact(r["views"]),
            _compact(r["channel_median"]),
            {"shorts": "Shorts", "all": "todos"}.get(r["median_source"], "—"),
            r["channel_title"] or "",
            r["title"] or "",
            f"[link={url}]{url}[/link]",
        )
    console.print(table)
