#!/usr/bin/env python3
"""
Fine-tuning (opzionale) di GLiNER sui TUOI dati — mini-suite agnostica.

Il NER di Sophia Vector usa GLiNER **zero-shot** (gliner-community v2.5): per la
maggior parte dei domini va benissimo SENZA addestramento. Questo script serve
solo se hai un dataset annotato di qualità e vuoi spremere qualità su un dominio
specifico (legale, medico, tecnico, …). Niente è hardcoded sul dominio: le label
sono quelle che compaiono nel tuo dataset.

Dati (JSONL, una riga per esempio). Due formati accettati, auto-rilevati:
  A) sorgente — comodo da scrivere/derivare a mano:
       {"text": "...", "entities": [["testo entità", "label"], ...]}
  B) nativo GLiNER — token già indicizzati (start/end INCLUSIVI sui token):
       {"tokenized_text": ["...", "..."], "ner": [[start, end, "label"], ...]}

Servono anche esempi NEGATIVI (entities/ner vuoti) o il modello vede entità ovunque.

Uso:
  # prova la pipeline col dataset demo incorporato, SENZA addestrare:
  python tuning/finetune_gliner.py --demo --dry-run
  # addestra sui tuoi dati (richiede: pip install accelerate):
  python tuning/finetune_gliner.py --data tuning/sample_dataset.jsonl --epochs 10
"""

import argparse
import importlib.util
import json
import random
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()

DEFAULT_BASE_MODEL = "gliner-community/gliner_small-v2.5"  # piccolo → tuning veloce
DEFAULT_OUT = "tuning/out/gliner-finetuned"  # fuori dal package models/

# Dataset demo: dominio NEUTRO e generico (nessun cliente/settore). Solo per
# vedere la pipeline girare end-to-end. Formato sorgente (text, [(span, label)]).
DEMO_EXAMPLES = [
    ("Marco Rossi è stato nominato direttore di Aurora Labs a Milano il 3 marzo 2026.",
     [("Marco Rossi", "persona"), ("Aurora Labs", "organizzazione"),
      ("Milano", "luogo"), ("3 marzo 2026", "data")]),
    ("Helena Vogt ha firmato l'accordo con Nordwind GmbH a Vienna.",
     [("Helena Vogt", "persona"), ("Nordwind GmbH", "organizzazione"), ("Vienna", "luogo")]),
    ("Il nuovo modello Orion sarà presentato alla conferenza di Berlino.",
     [("Orion", "prodotto"), ("Berlino", "luogo")]),
    ("Le vendite del prodotto Atlas sono cresciute nel 2025.",
     [("Atlas", "prodotto"), ("2025", "data")]),
    ("Sofia Bianchi interverrà al summit di Tokyo il 12 aprile.",
     [("Sofia Bianchi", "persona"), ("Tokyo", "luogo"), ("12 aprile", "data")]),
    ("Quantum Dynamics ha aperto una nuova sede a Singapore.",
     [("Quantum Dynamics", "organizzazione"), ("Singapore", "luogo")]),
    ("La riunione è stata rinviata a data da destinarsi.", []),          # negativo
    ("Il documento descrive procedure generali senza riferimenti propri.", []),  # negativo
]


# ============================================================
# Dataset: conversione (testo, entità) → formato GLiNER (indici token)
# ============================================================
def tokenize(text: str):
    """Tokenizzazione a parole+punteggiatura. Deve essere COERENTE tra costruzione
    del dataset e inferenza (qui GLiNER usa la sua, questa serve solo a indicizzare)."""
    return re.findall(r"\w+|[^\w\s]", text, re.UNICODE)


def build_example(text: str, spans: list) -> tuple[dict, int]:
    """(testo, [(stringa_entità, label)]) → ({tokenized_text, ner}, n_non_agganciate).
    Cerca la sottosequenza di token dell'entità nel testo. Se non la trova
    (tokenizzazione incoerente, entità inesistente) la salta e lo segnala."""
    tokens = tokenize(text)
    ner, missed = [], 0
    for entity_str, label in spans:
        ent_tokens = tokenize(entity_str)
        found = False
        for i in range(len(tokens) - len(ent_tokens) + 1):
            if tokens[i:i + len(ent_tokens)] == ent_tokens:
                ner.append([i, i + len(ent_tokens) - 1, label])  # end INCLUSIVO
                found = True
                break
        if not found:
            missed += 1
    return {"tokenized_text": tokens, "ner": ner}, missed


def load_dataset(path: Path) -> tuple[list, int]:
    """Legge un JSONL, auto-rilevando il formato (sorgente vs nativo GLiNER).
    Ritorna (esempi_in_formato_gliner, n_entità_non_agganciate)."""
    out, missed_total = [], 0
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            console.print(f"[red]Riga {ln}: JSON non valido ({e}) — saltata[/red]")
            continue
        if "tokenized_text" in row and "ner" in row:           # formato nativo
            out.append({"tokenized_text": row["tokenized_text"], "ner": row["ner"]})
        elif "text" in row:                                     # formato sorgente
            spans = [(s[0], s[1]) for s in row.get("entities", [])]
            ex, missed = build_example(row["text"], spans)
            out.append(ex)
            missed_total += missed
        else:
            console.print(f"[yellow]Riga {ln}: né 'tokenized_text' né 'text' — saltata[/yellow]")
    return out, missed_total


def labels_in(dataset: list) -> list:
    """Insieme ordinato delle label presenti (lo schema lo dà il dataset, non noi)."""
    return sorted({span[2] for ex in dataset for span in ex["ner"]})


# ============================================================
# UI
# ============================================================
def intro() -> None:
    console.print()
    console.print(Panel(
        "[bold]Fine-tuning di GLiNER — opzionale.[/bold]\n\n"
        "Il NER zero-shot ([cyan]gliner-community v2.5[/cyan]) copre già la maggior parte\n"
        "dei casi [bold]senza addestramento[/bold]. Allena solo se hai un dataset annotato\n"
        "di qualità e vuoi spremere qualità sul [italic]tuo[/italic] dominio.\n\n"
        "[dim]Lo script è agnostico: le label sono quelle del tuo dataset. La parte\n"
        "difficile non è qui — è costruire dati annotati buoni (con esempi negativi).[/dim]",
        title="[bold white]Sophia Vector[/bold white] [cyan]· GLiNER tuning[/cyan]",
        border_style="cyan", box=box.ROUNDED, padding=(1, 3), expand=False,
    ))


def config_table(args, n_train, n_eval, labels, missed) -> None:
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t.add_column(style="bright_black")
    t.add_column()
    t.add_row("base model", f"[white]{args.model}[/white]")
    t.add_row("dataset", f"[white]{args.data or 'demo (incorporato)'}[/white]")
    t.add_row("train / eval", f"[white]{n_train}[/white] / [white]{n_eval}[/white]")
    t.add_row("labels", f"[green]{', '.join(labels) or '—'}[/green]")
    t.add_row("device", f"[yellow]{'cpu' if args.cpu else 'auto (gpu se presente)'}[/yellow]")
    t.add_row("epoche", f"[white]{args.epochs}[/white]")
    t.add_row("output", f"[white]{args.out}[/white]")
    if missed:
        t.add_row("[red]entità non agganciate[/red]", f"[red]{missed}[/red] (controlla la tokenizzazione)")
    console.print(t)


def show_smoke(model, text, labels) -> None:
    t = Table(title="quick test", box=box.SIMPLE, title_style="bold")
    t.add_column("entità"); t.add_column("label", style="green"); t.add_column("score", justify="right")
    for e in model.predict_entities(text, labels):
        t.add_row(e["text"], e["label"], f"{e['score']:.2f}")
    console.print(t)


# ============================================================
# Main
# ============================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Fine-tuning opzionale di GLiNER (agnostico).")
    ap.add_argument("--data", help="JSONL del dataset (vedi docstring). Se assente usa il demo.")
    ap.add_argument("--demo", action="store_true", help="usa il dataset demo incorporato")
    ap.add_argument("--dry-run", action="store_true", help="prepara e valida il dataset, NON addestra")
    ap.add_argument("--model", default=DEFAULT_BASE_MODEL, help="modello GLiNER base")
    ap.add_argument("--out", default=DEFAULT_OUT, help="cartella di output del modello tunato")
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--lr", type=float, default=5e-6, help="LR basso: 5e-6..1e-5 tipico per GLiNER")
    ap.add_argument("--cpu", action="store_true", help="forza CPU")
    ap.add_argument("--eval-split", type=float, default=0.1)
    args = ap.parse_args()

    intro()

    # 1) dataset
    if args.data:
        path = Path(args.data)
        if not path.exists():
            console.print(f"[red]Dataset non trovato: {path}[/red]")
            sys.exit(1)
        dataset, missed = load_dataset(path)
    else:
        if not args.demo:
            console.print("[yellow]Nessun --data: uso il dataset demo. Per i tuoi dati: --data file.jsonl[/yellow]")
        dataset, missed = [], 0
        for text, spans in DEMO_EXAMPLES:
            ex, m = build_example(text, spans)
            dataset.append(ex); missed += m

    if not dataset:
        console.print("[red]Dataset vuoto.[/red]"); sys.exit(1)

    random.seed(42)
    random.shuffle(dataset)
    split = max(1, int(len(dataset) * (1 - args.eval_split)))
    train_data, eval_data = dataset[:split], dataset[split:] or dataset[:1]
    labels = labels_in(dataset)

    config_table(args, len(train_data), len(eval_data), labels, missed)

    if args.dry_run:
        console.print(Panel(
            "[bold]Dry-run:[/bold] dataset preparato e validato, niente training.\n"
            "Se 'entità non agganciate' è 0, gli indici token sono coerenti. "
            "Rilancia senza [cyan]--dry-run[/cyan] per addestrare.",
            border_style="green", box=box.ROUNDED, padding=(1, 2), expand=False,
        ))
        return

    # 2) accelerate è richiesto dal Trainer di HF
    if importlib.util.find_spec("accelerate") is None:
        console.print(Panel(
            "Per il training serve [bold]accelerate[/bold] (non installato):\n\n"
            "    [cyan].venv/bin/python -m pip install accelerate[/cyan]\n\n"
            "Nel frattempo puoi validare i dati con [cyan]--dry-run[/cyan].",
            title="[yellow]dipendenza mancante[/yellow]",
            border_style="yellow", box=box.ROUNDED, padding=(1, 2), expand=False,
        ))
        sys.exit(1)

    # 3) training
    from gliner import GLiNER
    from gliner.training import Trainer, TrainingArguments
    from gliner.data_processing.collator import SpanDataCollator

    console.print(f"[bright_black]Carico il modello base {args.model}…[/bright_black]")
    model = GLiNER.from_pretrained(args.model, load_tokenizer=True)
    collator = SpanDataCollator(model.config, data_processor=model.data_processor, prepare_labels=True)

    training_args = TrainingArguments(
        output_dir=args.out,
        learning_rate=args.lr,
        weight_decay=0.01,
        others_lr=1e-5,                 # LR (più alto) per la testa, separato dal backbone
        others_weight_decay=0.01,
        lr_scheduler_type="linear",
        warmup_ratio=0.1,
        per_device_train_batch_size=args.batch,
        per_device_eval_batch_size=args.batch,
        num_train_epochs=args.epochs,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        dataloader_num_workers=0,
        use_cpu=args.cpu,
        report_to="none",
    )
    trainer = Trainer(
        model=model, args=training_args,
        train_dataset=train_data, eval_dataset=eval_data,
        tokenizer=model.data_processor.transformer_tokenizer,
        data_collator=collator,
    )

    with console.status("[bold cyan]Training in corso…[/bold cyan]", spinner="dots"):
        trainer.train()

    final_dir = str(Path(args.out) / "final")
    model.save_pretrained(final_dir)
    console.print(f"[green]✅ modello tunato salvato in[/green] [white]{final_dir}[/white]")

    # 4) smoke test sulle label viste nel dataset
    sample = DEMO_EXAMPLES[0][0] if not args.data else None
    if sample and labels:
        tuned = GLiNER.from_pretrained(final_dir, load_tokenizer=True)
        show_smoke(tuned, sample, labels)
    console.print(f"\n[dim]Per usarlo nel worker: SOPHIA_VECTOR_GLINER_MODEL={final_dir}[/dim]")


if __name__ == "__main__":
    main()
