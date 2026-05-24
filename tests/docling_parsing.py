"""
Test Docling Parsing — invia un file al servizio Docling e mostra il documento convertito in Markdown.

Uso:
    python tests/docling_parsing.py .data/documento.pdf
    python tests/docling_parsing.py .data/documento.docx --async
"""

import sys
import os
import json
import time
import argparse

# Aggiungi root al path per importare utils
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from rich import box

from utils.docling import (
    upload_file_for_parsing_task_sync,
    upload_file_for_parsing_task_async,
    get_task_status,
    get_task_results,
)

console = Console()

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".data")


def save_response(file_path: str, result):
    """Salva la risposta JSON in .data/<nome_file>_parsing.json"""
    os.makedirs(DATA_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(file_path))[0]
    out_path = os.path.join(DATA_DIR, f"{base}_parsing.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    console.print(f"[bold green]Risposta salvata:[/bold green] {out_path}")


def run_sync(file_path: str):
    """Parsing sincrono."""
    console.rule("[bold cyan]Docling Parsing (sync)")
    console.print(f"File: [bold]{file_path}[/bold]")
    console.print()

    with console.status("[bold green]Invio file a Docling per parsing..."):
        start = time.time()
        result = upload_file_for_parsing_task_sync(file_path)
        elapsed = time.time() - start

    save_response(file_path, result)
    display_result(result, elapsed)


def run_async(file_path: str):
    """Parsing asincrono con polling."""
    console.rule("[bold cyan]Docling Parsing (async)")
    console.print(f"File: [bold]{file_path}[/bold]")
    console.print()

    with console.status("[bold green]Upload file..."):
        resp = upload_file_for_parsing_task_async(file_path)

    task_id = resp.get("task_id")
    if not task_id:
        console.print("[bold red]Errore: nessun task_id nella risposta[/bold red]")
        console.print(resp)
        return

    console.print(f"Task ID: [bold yellow]{task_id}[/bold yellow]")

    start = time.time()
    with console.status("[bold green]Attendo completamento...") as status:
        while True:
            poll = get_task_status(task_id)
            task_status = poll.get("task_status", "UNKNOWN")
            status.update(f"[bold green]Stato: {task_status}...")

            if task_status == "SUCCESS":
                break
            elif task_status in ("FAILURE", "REVOKED"):
                console.print(f"[bold red]Task fallito: {task_status}[/bold red]")
                console.print(poll)
                return

            time.sleep(2)

    with console.status("[bold green]Recupero risultati..."):
        result = get_task_results(task_id)

    elapsed = time.time() - start
    save_response(file_path, result)
    display_result(result, elapsed)


def display_result(result: dict, elapsed: float):
    """Mostra il risultato del parsing."""
    # Naviga nella struttura della risposta per trovare il markdown
    md_content = None
    pages = 0

    if isinstance(result, dict):
        # Formato: {document: [{md_content: ...}]} o {output: {md_content: ...}}
        docs = result.get("document", result.get("output", result.get("documents", [])))

        if isinstance(docs, list) and docs:
            doc = docs[0] if isinstance(docs[0], dict) else {}
        elif isinstance(docs, dict):
            doc = docs
        else:
            doc = result

        # Cerca il markdown in vari formati possibili
        md_content = (
            doc.get("md_content")
            or doc.get("markdown")
            or doc.get("content")
            or doc.get("text")
        )
        pages = doc.get("num_pages", doc.get("pages", 0))

    # Statistiche
    console.print()
    stats = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    stats.add_column(style="bold")
    stats.add_column()
    stats.add_row("Tempo", f"{elapsed:.1f}s")
    if pages:
        stats.add_row("Pagine", str(pages))
    if md_content:
        stats.add_row("Lunghezza output", f"{len(md_content)} chars")
        stats.add_row("Righe", str(md_content.count('\n') + 1))
    console.print(Panel(stats, title="[bold]Statistiche", border_style="cyan"))

    if md_content:
        # Mostra il markdown con syntax highlighting
        syntax = Syntax(md_content, "markdown", theme="monokai", word_wrap=True)
        console.print(Panel(syntax, title="[bold]Markdown Output", border_style="green", expand=True))
    else:
        console.print("[bold yellow]Nessun contenuto markdown trovato nella risposta.[/bold yellow]")
        # Mostra la risposta raw troncata
        import json
        raw = json.dumps(result, indent=2, ensure_ascii=False)
        console.print(Panel(raw[:3000], title="Risposta raw", border_style="dim"))


def main():
    parser = argparse.ArgumentParser(description="Test Docling parsing service")
    parser.add_argument("file", help="Path al file da processare (pdf, docx, ...)")
    parser.add_argument("--async", dest="use_async", action="store_true", help="Usa modalita' asincrona")
    args = parser.parse_args()

    file_path = os.path.abspath(args.file)
    if not os.path.isfile(file_path):
        console.print(f"[bold red]File non trovato: {file_path}[/bold red]")
        sys.exit(1)

    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    console.print(f"[dim]Dimensione file: {size_mb:.2f} MB[/dim]")

    if args.use_async:
        run_async(file_path)
    else:
        run_sync(file_path)


if __name__ == "__main__":
    main()
