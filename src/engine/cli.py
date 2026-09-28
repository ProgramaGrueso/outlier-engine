"""CLI `engine`."""

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from engine import db
from engine import doctor as doctor_mod
from engine import research as research_mod

app = typer.Typer(help="Outlier Engine: formatos virales → guiones → ComfyUI.", no_args_is_help=True)
console = Console()


def _compact(n: float | None) -> str:
    if n is None:
        return "—"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suffix}"
    return f"{n:.0f}"


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
    niche: str = typer.Argument(..., help='Búsqueda del nicho, ej. "frutas ia"'),
    days: int = typer.Option(30, help="Solo Shorts publicados en los últimos N días"),
    pages: int = typer.Option(2, help="Páginas de search.list (50 resultados, 100 unidades c/u)"),
    order: str = typer.Option("viewCount", help="viewCount | relevance | date"),
    lang: Optional[str] = typer.Option(None, help="relevanceLanguage, ej. es"),
    region: Optional[str] = typer.Option(None, help="regionCode, ej. MX"),
    min_ratio: Optional[float] = typer.Option(None, help="Umbral de outlier (por defecto OUTLIER_RATIO_MIN)"),
) -> None:
    """Busca Shorts recientes del nicho y guarda los outliers."""
    if order not in ("viewCount", "relevance", "date"):
        raise typer.BadParameter("order debe ser viewCount, relevance o date")
    try:
        s = research_mod.research(
            niche, days=days, pages=pages, order=order, lang=lang, region=region, min_ratio=min_ratio
        )
    except research_mod.QuotaExceededError as e:
        console.print(f"[red]Cuota agotada:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(
        f"[bold]{s.niche}[/bold]: {s.videos_scanned} videos · {s.shorts} Shorts · "
        f"canales {s.channels_fetched} nuevos / {s.channels_cached} en caché / {s.channels_skipped} sin mediana\n"
        f"[green]{s.outliers_saved} outliers guardados[/green] · {s.units_used} unidades de cuota "
        f"(hoy: {db.api_units_today(db.get_conn())})"
    )


@app.command()
def top(
    limit: int = typer.Option(20, help="Cuántos mostrar"),
    niche: Optional[str] = typer.Option(None, help="Filtrar por nicho"),
) -> None:
    """Outliers ordenados por ratio (vistas / mediana del canal)."""
    rows = research_mod.top(db.get_conn(), limit=limit, niche=niche)
    if not rows:
        console.print("Sin outliers todavía. Corre `engine research \"<nicho>\"` primero.")
        return
    table = Table(title=f"Top {len(rows)} outliers")
    table.add_column("#", justify="right")
    table.add_column("Ratio", justify="right", style="bold green")
    table.add_column("Vistas", justify="right")
    table.add_column("Mediana", justify="right")
    table.add_column("Canal", max_width=20, no_wrap=True)
    table.add_column("Título", max_width=40, no_wrap=True)
    table.add_column("Link", no_wrap=True)
    for i, r in enumerate(rows, 1):
        url = research_mod.short_url(r["video_id"])
        table.add_row(
            str(i),
            f"{r['outlier_ratio']:.1f}×",
            _compact(r["views"]),
            _compact(r["channel_median"]),
            r["channel_title"] or "",
            r["title"] or "",
            f"[link={url}]{url}[/link]",
        )
    console.print(table)
