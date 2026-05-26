"""
Frontispiece di avvio: un banner rich con app, servizi esterni e modelli di
ingestion. Puramente cosmetico — best-effort: se rich non è disponibile o il
rendering fallisce, si degrada a un print minimale senza mai rompere lo startup.

Le credenziali (password Mongo/Falkor) NON vengono mai mostrate.
"""

from urllib.parse import urlsplit

from utils.config import settings


def _mongo_display(uri: str) -> str:
    """`mongodb://user:pass@host:27017/db` → `db @ host:27017` (no credenziali)."""
    try:
        s = urlsplit(uri)
        host = s.hostname or "?"
        port = f":{s.port}" if s.port else ""
        db = (s.path or "/").lstrip("/").split("?")[0] or "?"
        return f"{db} @ {host}{port}"
    except Exception:
        return "configured"


def _dot(on: bool) -> str:
    return "[green]●[/green]" if on else "[bright_black]○[/bright_black]"

_WORDMARK = r"""
  ___           _    _          _   ___ 
 / __| ___ _ __| |_ (_)__ _    /_\ |_ _|
 \__ \/ _ \ '_ \ ' \| / _` |  / _ \ | | 
 |___/\___/ .__/_||_|_\__,_| /_/ \_\___|
          |_|                           
"""

def render_banner() -> None:
    """Stampa il frontispiece. Best-effort: fallback a print se rich manca."""
    try:
        from rich.console import Console, Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        from rich import box

        console = Console()

        wordmark = Text(_WORDMARK, style="bold cyan")
        subtitle = Text.from_markup(
            "[bold white]Vector[/bold white] [dim]· OpenAI-compatible store · "
            "Qdrant + FalkorDB graph + GLiNER[/dim]"
        )

        # Griglia di sole coppie label/valore → prima colonna stretta e allineata.
        grid = Table.grid(padding=(0, 3))
        grid.add_column(justify="left", style="bright_black", no_wrap=True)  # label
        grid.add_column(justify="left")                                       # value

        grid.add_row("[bold]Services[/bold]", "")
        grid.add_row("  Qdrant", f"[white]{settings.QDRANT_URL}[/white]")
        grid.add_row("  MongoDB", f"[white]{_mongo_display(settings.MONGODB_URI)}[/white]")
        grid.add_row("  Docling", f"[white]{settings.DOCLING_URL}[/white]")
        grid.add_row("  Embeddings", f"[white]{settings.EMBEDDINGS_URL}[/white]")
        grid.add_row(
            "  FalkorDB",
            f"[white]{settings.FALKOR_HOST}:{settings.FALKOR_PORT}[/white]  {_dot(settings.GRAPH_ENABLED)}",
        )
        grid.add_row("", "")
        grid.add_row("[bold]Ingestion models[/bold]", "")
        if settings.GLINER_ENABLED:
            grid.add_row(
                "  NER",
                f"[white]{settings.GLINER_MODEL}[/white]  [bright_black]·[/bright_black]  "
                f"[yellow]{settings.GLINER_DEVICE}[/yellow]  {_dot(True)}",
            )
        else:
            grid.add_row("  NER", f"[bright_black]regex-only[/bright_black]  {_dot(False)}")
        if settings.RELATIONS_ENABLED:
            grid.add_row(
                "  Relations",
                f"[white]{settings.RELATIONS_MODEL}[/white]  [bright_black]·[/bright_black]  "
                f"[yellow]{settings.GLINER_DEVICE}[/yellow]  {_dot(True)}",
            )
        else:
            grid.add_row("  Relations", f"[bright_black]off (lazy)[/bright_black]  {_dot(False)}")
        if settings.CLASSIFIER_ENABLED:
            grid.add_row(
                "  Classifier",
                f"[white]{settings.CLASSIFIER_MODEL}[/white]  [bright_black]·[/bright_black]  "
                f"[yellow]{settings.GLINER_DEVICE}[/yellow]  {_dot(True)}",
            )
        else:
            grid.add_row("  Classifier", f"[bright_black]off (opt-in)[/bright_black]  {_dot(False)}")
        if settings.ASR_ENABLED:
            grid.add_row(
                "  ASR",
                f"[white]whisper:{settings.ASR_MODEL}[/white]  [bright_black]·[/bright_black]  "
                f"[yellow]{settings.ASR_DEVICE}[/yellow]  {_dot(True)}",
            )
        else:
            grid.add_row("  ASR", f"[bright_black]off[/bright_black]  {_dot(False)}")

        panel = Panel(
            Group(wordmark, subtitle, Text(""), grid),
            title=f"[bold white]{settings.APP_NAME}[/bold white] [cyan]v{settings.APP_VERSION}[/cyan]",
            border_style="cyan",
            box=box.ROUNDED,
            padding=(1, 3),
            expand=False,
        )
        console.print()
        console.print(panel)
        console.print()
    except Exception:
        # Fallback minimale: mai far fallire lo startup per il banner.
        print("\n" + "=" * 60)
        print(f"✨ {settings.APP_NAME} {settings.APP_VERSION}")
        print("=" * 60 + "\n")
