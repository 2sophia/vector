"""
Test Docling Chunking — invia un file al servizio Docling e mostra i chunk risultanti.

Uso:
    python tests/docling_chunking.py .data/documento.pdf
    python tests/docling_chunking.py .data/documento.docx
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
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from utils.docling import (
    upload_file_for_chunking_sync,
    upload_file_for_chunking_task_async,
    get_task_status,
    get_task_results,
)

console = Console()

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".data")


def save_response(file_path: str, result):
    """Salva la risposta JSON in .data/<nome_file>_chunking.json"""
    os.makedirs(DATA_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(file_path))[0]
    out_path = os.path.join(DATA_DIR, f"{base}_chunking.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    console.print(f"[bold green]Risposta salvata:[/bold green] {out_path}")


def run_sync(file_path: str):
    """Chunking sincrono."""
    console.rule("[bold cyan]Docling Chunking (sync)")
    console.print(f"File: [bold]{file_path}[/bold]")
    console.print()

    with console.status("[bold green]Invio file a Docling per chunking..."):
        start = time.time()
        result = upload_file_for_chunking_sync(file_path)
        elapsed = time.time() - start

    save_response(file_path, result)
    display_chunks(result, elapsed)


def run_async(file_path: str):
    """Chunking asincrono con polling."""
    console.rule("[bold cyan]Docling Chunking (async)")
    console.print(f"File: [bold]{file_path}[/bold]")
    console.print()

    with console.status("[bold green]Upload file..."):
        resp = upload_file_for_chunking_task_async(file_path)

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
    display_chunks(result, elapsed)


def display_chunks(result: dict, elapsed: float):
    """Mostra i chunk in una tabella rich."""
    # Estrai i chunk dalla risposta
    chunks = []

    # Formato diretto: lista di chunk
    if isinstance(result, list):
        chunks = result
    # Formato con output -> chunks
    elif isinstance(result, dict):
        output = result.get("output", result)
        if isinstance(output, dict):
            chunks = output.get("chunks", [])
        elif isinstance(output, list):
            chunks = output

    if not chunks:
        console.print("[bold yellow]Nessun chunk trovato nella risposta.[/bold yellow]")
        console.print(Panel(str(result)[:2000], title="Risposta raw", border_style="dim"))
        return

    # Statistiche
    console.print()
    stats = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    stats.add_column(style="bold")
    stats.add_column()
    stats.add_row("Chunk totali", str(len(chunks)))
    stats.add_row("Tempo", f"{elapsed:.1f}s")

    text_lengths = []
    for c in chunks:
        text = c.get("text", c.get("content", ""))
        text_lengths.append(len(text))

    if text_lengths:
        stats.add_row("Lunghezza media", f"{sum(text_lengths) / len(text_lengths):.0f} chars")
        stats.add_row("Min / Max", f"{min(text_lengths)} / {max(text_lengths)} chars")

    console.print(Panel(stats, title="[bold]Statistiche", border_style="cyan"))

    # Tabella chunk
    table = Table(
        title="Chunks",
        box=box.ROUNDED,
        show_lines=True,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Testo", ratio=4)
    table.add_column("Meta", ratio=1, style="dim")
    table.add_column("Chars", width=6, justify="right")

    for i, chunk in enumerate(chunks, 1):
        text = chunk.get("text", chunk.get("content", ""))
        preview = text[:300] + ("..." if len(text) > 300 else "")

        # Raccogli metadata utili
        meta_parts = []
        for key in ("page", "page_no", "heading", "label", "type"):
            val = chunk.get(key) or chunk.get("meta", {}).get(key)
            if val is not None:
                meta_parts.append(f"{key}={val}")

        table.add_row(str(i), preview, "\n".join(meta_parts), str(len(text)))

    console.print(table)


def main():
    parser = argparse.ArgumentParser(description="Test Docling chunking service")
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
