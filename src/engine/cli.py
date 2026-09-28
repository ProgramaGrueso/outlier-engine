"""CLI `engine`."""

import typer
from rich.console import Console
from rich.table import Table

from engine import doctor as doctor_mod

app = typer.Typer(help="Outlier Engine: formatos virales → guiones → ComfyUI.", no_args_is_help=True)
console = Console()


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
        table.add_row("✅" if c.ok else "❌", c.name, c.detail)
    console.print(table)
    if not all(c.ok for c in checks):
        raise typer.Exit(code=1)
