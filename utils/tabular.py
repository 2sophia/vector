"""
Chunker tabulare (CSV/XLSX) — bypassa Docling per i file a tabella.

Docling tratta un CSV/XLSX come UNA tabella: su file con decine di migliaia di
righe la serializzazione esplode e il chunker non emette nulla (0 chunk → il job
falliva con "nessun contenuto estraibile"). Ma una tabella di dati NON è prosa:
embeddare N righe quasi-identiche è rumore e la domanda "qual è il valore della
riga X" è un job da filtro/SQL, non da ricerca semantica. Qui produciamo chunk ad
alta resa, deterministici, senza LLM:

  1) una "table card" (SEMPRE, 1 chunk): schema (colonna + tipo inferito),
     dimensioni, statistiche per colonna (numeriche → min/max/media/somma;
     categoriche → cardinalità + valori più frequenti) e righe campione. È una
     compressione semantica della tabella: chi cerca "che dati abbiamo su …" la trova.
  2) le righe verbalizzate in batch ("colA: v; colB: v; …") fino a un cap → i
     singoli record restano trovabili senza gonfiare l'indice. Oltre il cap viene
     DICHIARATO (niente troncamento muto): la card resta il riepilogo completo.

Output: lo stesso shape di Docling — {"chunks": [{text, chunk_index, headings,
page_numbers}, …]} — così il worker e tutti i layer a valle (embed, NER, grafo,
Qdrant) non cambiano di una riga.

Engine AGNOSTICO: nessun nome di colonna o dominio è hardcoded, tutto viene dal
file. I knob stanno in config (SOPHIA_VECTOR_TABULAR_*).
"""

import os
import csv as _csv
import logging

from .settings import (
    TABULAR_ROWS_PER_CHUNK,
    TABULAR_MAX_ROW_CHUNKS,
    TABULAR_SAMPLE_ROWS,
    TABULAR_TOP_CATEGORIES,
    TABULAR_MAX_READ_MB,
)

logger = logging.getLogger("tabular")

# Estensioni gestite QUI invece che da Docling. NB: .xls/.ods/.xlsx-da-conversione
# arrivano già come .xlsx (utils/convert._OFFICE_TARGET) → al momento del chunking
# il path è sempre .csv o .xlsx.
TABULAR_EXTENSIONS = {".csv", ".xlsx"}

# Frazione di valori che devono parsare come numero perché una colonna sia
# trattata come numerica (sennò è categorica). Robusto a colonne sporche.
_NUMERIC_FRACTION = 0.8
# Clip dei valori lunghi nella verbalizzazione/righe campione (caratteri).
_CELL_CLIP = 80
_CAT_CLIP = 48


def is_tabular(path: str) -> bool:
    """True se il file va chunkato dal layer tabulare invece che da Docling."""
    return os.path.splitext(path)[1].lower() in TABULAR_EXTENSIONS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clip(s: str, n: int = _CELL_CLIP) -> str:
    s = " ".join(str(s).split())  # collassa whitespace (le celle sono space-padded)
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if x != x:  # NaN
        return "—"
    if x == int(x):
        return f"{int(x):,}".replace(",", ".")  # 1234567 → 1.234.567
    return f"{x:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")  # it: 1.234,56


def _to_numeric(series, decimal: str):
    """Coerce una colonna (letta come stringa) a numerico, gestendo il dialetto
    italiano (separatore migliaia '.', decimale ','). Best-effort: i non-numerici
    diventano NaN. Restituisce una Series float."""
    import pandas as pd

    s = series.astype(str).str.strip()
    if decimal == ",":
        # 1.234,56 → 1234.56 ; toglie i separatori migliaia poi normalizza il decimale
        s = s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    # toglie spazi interni residui (numeri space-padded) e simboli valuta comuni
    s = s.str.replace(r"[\s ]", "", regex=True).str.replace("%", "", regex=False)
    return pd.to_numeric(s, errors="coerce")


def _column_stats(df, decimal: str) -> list:
    import pandas as pd

    lines = []
    n = len(df)
    for col in df.columns:
        num = _to_numeric(df[col], decimal)
        valid = int(num.notna().sum())
        if n and valid >= max(1, int(_NUMERIC_FRACTION * n)):
            d = num.dropna()
            lines.append(
                f"- {col} (num): min={_fmt(d.min())}, max={_fmt(d.max())}, "
                f"media={_fmt(d.mean())}, somma={_fmt(d.sum())}, valorizzati={valid}/{n}"
            )
        else:
            s = df[col].astype(str).str.strip()
            s = s[s != ""]
            distinct = int(s.nunique())
            top = s.value_counts().head(TABULAR_TOP_CATEGORIES)
            top_str = "; ".join(f"{_clip(str(v), _CAT_CLIP)} ({c})" for v, c in top.items())
            lines.append(f"- {col} (cat): {distinct} valori distinti — frequenti: {top_str}")
    return lines


def _verbalize(row, columns) -> str:
    """Una riga → "col: val; col: val", saltando le celle vuote. Self-contained:
    ogni riga porta il nome delle colonne → resta ricercabile da sola."""
    parts = []
    for c in columns:
        v = str(row[c]).strip()
        if v:
            parts.append(f"{c}: {_clip(v)}")
    return "; ".join(parts)


def _table_chunks(df, label: str, decimal: str, start_index: int) -> list:
    """Costruisce card + righe verbalizzate per UNA tabella (un foglio)."""
    import pandas as pd

    nrows, ncols = df.shape
    cols = list(df.columns)
    chunks = []

    # --- 1) table card -----------------------------------------------------
    card = [
        f"Tabella «{label}» — {nrows} righe × {ncols} colonne.",
        "Colonne: " + ", ".join(str(c) for c in cols) + ".",
        "",
        "Statistiche per colonna:",
    ]
    card += _column_stats(df, decimal) if nrows else ["- (tabella senza righe)"]
    sample_n = min(TABULAR_SAMPLE_ROWS, nrows)
    if sample_n:
        card += ["", f"Righe campione (prime {sample_n}):"]
        for _, r in df.head(sample_n).iterrows():
            line = _verbalize(r, cols)
            if line:
                card.append("• " + line)
    chunks.append({
        "text": "\n".join(card),
        "headings": [f"Riepilogo tabella: {label}"],
        "page_numbers": [],
        "chunk_index": start_index,
    })

    # --- 2) righe verbalizzate (gated dal cap) -----------------------------
    idx = start_index + 1
    emitted = 0
    if TABULAR_ROWS_PER_CHUNK > 0 and TABULAR_MAX_ROW_CHUNKS != 0 and nrows:
        cap_rows = nrows if TABULAR_MAX_ROW_CHUNKS < 0 else min(
            nrows, TABULAR_MAX_ROW_CHUNKS * TABULAR_ROWS_PER_CHUNK
        )
        for bstart in range(0, cap_rows, TABULAR_ROWS_PER_CHUNK):
            batch = df.iloc[bstart: bstart + TABULAR_ROWS_PER_CHUNK]
            lines = [v for _, r in batch.iterrows() if (v := _verbalize(r, cols))]
            if not lines:
                continue
            end = bstart + len(batch)
            chunks.append({
                "text": "\n".join(lines),
                "headings": [f"{label} — righe {bstart + 1}–{end}"],
                "page_numbers": [],
                "chunk_index": idx,
            })
            idx += 1
            emitted = end

    # --- 3) dichiarazione di copertura se troncato (no troncamento muto) ----
    if emitted < nrows:
        note = (
            f"Copertura indice: della tabella «{label}» sono indicizzate come righe "
            f"{emitted} di {nrows} (cap configurato). Le restanti {nrows - emitted} "
            f"righe non sono nell'indice riga-per-riga; la table card resta il "
            f"riepilogo statistico completo dell'intera tabella."
        )
        logger.info(f"tabular «{label}»: indicizzate {emitted}/{nrows} righe (cap)")
        chunks.append({
            "text": note,
            "headings": [f"{label} — copertura indice"],
            "page_numbers": [],
            "chunk_index": idx,
        })

    return chunks


# ---------------------------------------------------------------------------
# Lettura robusta
# ---------------------------------------------------------------------------

def _sniff_csv(path: str):
    """Inferisce encoding + delimitatore + decimale da un campione del file.
    Restituisce (encoding, delimiter, decimal)."""
    with open(path, "rb") as f:
        raw = f.read(65536)
    enc = "utf-8"
    try:
        sample = raw.decode("utf-8")
    except UnicodeDecodeError:
        enc, sample = "latin-1", raw.decode("latin-1", errors="replace")

    delim = ","
    try:
        delim = _csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except Exception:
        first = next((ln for ln in sample.splitlines() if ln.strip()), "")
        if first:
            delim = max(";,\t|", key=first.count)
    # Convenzione robusta: col separatore ';' i decimali sono quasi sempre con la
    # virgola (dialetto IT/EU); con ',' come separatore il decimale è il punto.
    decimal = "," if delim == ";" else "."
    return enc, delim, decimal


def _read_csv(path: str):
    """Legge un CSV come stringhe (niente type-guessing di pandas su dati sporchi:
    l'inferenza numerica la facciamo noi, uniforme). Sui file enormi legge solo un
    campione per non saturare la RAM. Restituisce (df, decimal, sampled)."""
    import pandas as pd

    enc, delim, decimal = _sniff_csv(path)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    sampled = size_mb > TABULAR_MAX_READ_MB
    # peek abbastanza ampio da coprire card + righe entro il cap
    peek = max(50_000, abs(TABULAR_MAX_ROW_CHUNKS) * TABULAR_ROWS_PER_CHUNK + TABULAR_SAMPLE_ROWS)
    df = pd.read_csv(
        path, sep=delim, dtype=str, encoding=enc,
        nrows=(peek if sampled else None),
        on_bad_lines="skip", skipinitialspace=True,
        keep_default_na=False, na_values=[""],
    ).fillna("")
    return df, decimal, sampled


def chunk_tabular(path: str, max_tokens=None) -> dict:
    """Chunka un CSV/XLSX nel formato Docling-compatibile. `max_tokens` è accettato
    per parità di firma col chunker Docling ma qui non serve (la dimensione dei
    chunk è governata dai knob righe/chunk)."""
    import pandas as pd

    ext = os.path.splitext(path)[1].lower()
    base = os.path.basename(path)
    chunks = []

    try:
        if ext == ".csv":
            df, decimal, sampled = _read_csv(path)
            label = base + (" (campione)" if sampled else "")
            chunks = _table_chunks(df, label, decimal, 0)
        elif ext == ".xlsx":
            # tutti i fogli; ognuno è una tabella a sé (card + righe)
            frames = pd.read_excel(path, sheet_name=None, dtype=str)
            start = 0
            multi = len(frames) > 1
            for sheet, df in frames.items():
                df = df.fillna("")
                label = f"{base} · {sheet}" if multi else base
                ck = _table_chunks(df, label, ".", start)
                chunks += ck
                start += len(ck)
        else:
            return {"chunks": []}
    except pd.errors.EmptyDataError:
        logger.warning(f"tabular: {base} vuoto/illeggibile → 0 chunk")
        return {"chunks": []}

    return {"chunks": chunks}
