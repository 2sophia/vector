"""
FalkorDB knowledge graph — layer ADDITIVO sopra la pipeline Qdrant.

Per ogni `vector_store_id` esiste un grafo omonimo (simmetria con la collection
Qdrant → isolamento naturale tra store). Dentro, ogni nodo Document/Chunk porta
lo `slug` della directory, così l'espansione di vicinato (graph-augmented
retrieval) può confinarsi per-directory senza sconfinare.

Schema strutturale (M2, deterministico, niente LLM):

    (:Document {id=file_id, filename, slug, content_hash, ingested_at})
    (:Section  {id, title, level, path, slug})
    (:Chunk    {id, chunk_index, page, qdrant_point_id, text, slug})

    (:Document)-[:HAS_SECTION]->(:Section)        # sezione di 1° livello
    (:Section)-[:HAS_SUBSECTION]->(:Section)       # gerarchia headings
    (:Section)-[:HAS_CHUNK]->(:Chunk)              # foglia → chunk
    (:Document)-[:HAS_CHUNK]->(:Chunk)             # fallback: chunk senza headings
    (:Chunk)-[:NEXT]->(:Chunk)                     # ordine di lettura

Tutto best-effort: gli errori vengono loggati ma NON propagati, così un guasto
del grafo non rompe mai l'ingestion Qdrant (che resta la pipeline primaria).
Le funzioni sono sincrone: chiamarle via `asyncio.to_thread` dal worker.
"""

import re
from typing import Any, Dict, List, Optional

from utils.logger import get_logger
from utils.settings import (
    GRAPH_ENABLED, FALKOR_HOST, FALKOR_PORT, FALKOR_PASSWORD, FALKOR_GRAPH_PREFIX,
    CURATION_GRAPH_LINK,
)

logger = get_logger(__name__)

_SECTION_SEP = " › "

# Client FalkorDB lazy/singleton (redis sotto → sincrono, thread-safe a sufficienza).
_db = None
_db_failed = False  # se la prima connessione fallisce non riprovo a ogni chunk


def _get_db():
    """Ritorna il client FalkorDB, o None se il grafo è disabilitato/irraggiungibile."""
    global _db, _db_failed
    if not GRAPH_ENABLED or _db_failed:
        return None
    if _db is not None:
        return _db
    try:
        from falkordb import FalkorDB

        _db = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT, password=FALKOR_PASSWORD or None)
        # ping di cortesia: forza una connessione vera
        _db.connection.ping()
        logger.info(f"FalkorDB connesso ({FALKOR_HOST}:{FALKOR_PORT})")
        return _db
    except Exception as e:
        _db_failed = True
        logger.warning(f"FalkorDB non raggiungibile, grafo disattivato per questa sessione: {e}")
        return None


def graph_enabled() -> bool:
    return _get_db() is not None


def _graph(vector_store_id: str):
    db = _get_db()
    if db is None:
        return None
    # namespace per-progetto: una stessa istanza FalkorDB può ospitare più progetti
    return db.select_graph(f"{FALKOR_GRAPH_PREFIX}{vector_store_id}")


def graph_stats(vector_store_id: str) -> Dict[str, Any]:
    """Conteggi leggeri del knowledge graph di un vector store (per la pagina Vectors):
    documenti, chunk, entità, menzioni, relazioni tipizzate. Query count (non esporta
    il grafo). Best-effort: {"graph_enabled": False} se FalkorDB è off/irraggiungibile."""
    g = _graph(vector_store_id)
    if g is None:
        return {"graph_enabled": False}

    def _cnt(q):
        try:
            res = g.query(q)
            return res.result_set[0][0] if res.result_set else 0
        except Exception:
            return 0

    return {
        "graph_enabled": True,
        "documents": _cnt("MATCH (d:Document) RETURN count(d)"),
        "chunks": _cnt("MATCH (c:Chunk) RETURN count(c)"),
        "entities": _cnt("MATCH (e:Entity) RETURN count(e)"),
        "mentions": _cnt("MATCH ()-[r:MENTIONS]->() RETURN count(r)"),
        "relations": _cnt("MATCH ()-[r:REL]->() RETURN count(r)"),
    }


def find_relation_conflicts(
    vector_store_id: str, limit: int = 50, max_values: int = 8
) -> Dict[str, Any]:
    """SOLA LETTURA: coppie (entità, tipo-relazione) che puntano a PIÙ valori
    distinti — candidati conflitto da rivedere. Non-LLM, agnostico: sfrutta solo le
    relazioni tipizzate già estratte (:Entity)-[:REL {type}]->(:Entity).

    ATTENZIONE (coerente con lo SOTA: anche WikiCollide fa AUROC ~75% → umano nel
    loop): una relazione MULTIVALORE è spesso legittima (un soggetto può avere più
    oggetti per la stessa relazione). Qui NON si asserisce una contraddizione e NON
    si cancella nulla: si SEGNALANO le coppie con valori multipli perché l'operatore
    le verifichi. È il primo gradino del type-5 (verità/coerenza) senza modelli.

    Ritorna `{graph_enabled, conflicts, samples:[{head, head_type, relation,
    values:[...], value_count}]}`. Best-effort: degrada a graph_enabled=False."""
    g = _graph(vector_store_id)
    if g is None:
        return {"graph_enabled": False, "conflicts": 0, "samples": []}
    try:
        total = g.query(
            """
            MATCH (h:Entity)-[r:REL]->(t:Entity)
            WITH h, r.type AS rtype, count(DISTINCT t.id) AS n
            WHERE n >= 2
            RETURN count(*)
            """
        )
        conflicts = total.result_set[0][0] if total.result_set else 0

        rows = g.query(
            """
            MATCH (h:Entity)-[r:REL]->(t:Entity)
            WITH h, r.type AS rtype,
                 collect(DISTINCT t.name) AS values, count(DISTINCT t.id) AS n
            WHERE n >= 2
            RETURN h.name AS head, h.type AS htype, rtype, values, n
            ORDER BY n DESC
            LIMIT $limit
            """,
            {"limit": limit},
        ).result_set or []

        samples = []
        for head, htype, rtype, values, n in rows:
            vals = [v for v in (values or []) if v][:max_values]
            samples.append({"head": head, "head_type": htype, "relation": rtype,
                            "values": vals, "value_count": int(n)})
        return {"graph_enabled": True, "conflicts": int(conflicts), "samples": samples}
    except Exception as e:
        logger.warning(f"find_relation_conflicts({vector_store_id}) failed: {e}")
        return {"graph_enabled": True, "conflicts": 0, "samples": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Purge (re-ingest sicuro)
# ---------------------------------------------------------------------------

def purge_file_graph(vector_store_id: str, file_id: str) -> None:
    """Rimuove Document/Section/Chunk di un file_id (archi inclusi). Le :Entity
    condivise NON vengono toccate. Best-effort."""
    g = _graph(vector_store_id)
    if g is None:
        return
    try:
        g.query(
            """
            MATCH (n)
            WHERE (n:Document OR n:Section OR n:Chunk)
              AND (n.id = $fid OR n.id STARTS WITH $pre)
            DETACH DELETE n
            """,
            {"fid": file_id, "pre": f"{file_id}::"},
        )
        # relazioni tipizzate (M5): togli questo file dalla provenienza dell'arco,
        # poi elimina gli archi rimasti senza alcun file (re-ingest sicuro).
        g.query(
            "MATCH ()-[r:REL]->() WHERE $fid IN r.files "
            "SET r.files = [f IN r.files WHERE f <> $fid]",
            {"fid": file_id},
        )
        g.query("MATCH ()-[r:REL]->() WHERE r.files IS NULL OR size(r.files) = 0 DELETE r")
        # NB: le due query orfani qui sotto scandiscono l'INTERO grafo dello store, non
        # solo il file → costo O(grafo) per ogni file ingerito. Accettabile sui corpus
        # attuali; se il grafo crescesse molto, limitare lo scope alle entità toccate.
        # entità rimaste senza menzioni NÉ relazioni → orfane, si rimuovono
        g.query("MATCH (e:Entity) WHERE NOT (e)<-[:MENTIONS]-() AND NOT (e)-[:REL]-() DELETE e")
        # nodi :Content (curation) rimasti senza chunk → orfani, si rimuovono
        g.query("MATCH (ct:Content) WHERE NOT (ct)<-[:SAME_CONTENT]-() DELETE ct")
    except Exception as e:
        logger.warning(f"purge_file_graph({vector_store_id}, {file_id}) failed: {e}")


def delete_graph(vector_store_id: str) -> None:
    """Cancella l'intero grafo di un vector store (usato a delete dello store)."""
    g = _graph(vector_store_id)
    if g is None:
        return
    try:
        g.delete()
    except Exception as e:
        logger.warning(f"delete_graph({vector_store_id}) failed: {e}")


# ---------------------------------------------------------------------------
# Optimize: pulizia post-hoc del grafo (NO re-ingest)
# ---------------------------------------------------------------------------

# Entità "spazzatura": solo cifre/punteggiatura (numerazioni tipo "1.6.1.12.3",
# "12", "3.4"). Filtro AGNOSTICO. FalkorDB non supporta `=~` → si applica in Python.
_NUMERICISH_RE = re.compile(r"^[0-9.,;:()\-/ ]+$")


def _is_junk_entity(name: str, min_len: int, drop_numeric: bool = True) -> bool:
    n = (name or "").strip()
    if len(n) < min_len:
        return True
    if drop_numeric and _NUMERICISH_RE.match(n):
        return True
    return False


def optimize_graph(
    vector_store_id: str,
    min_score: float = 0.6,
    min_entity_len: int = 3,
    drop_numeric: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Ripulisce il grafo entità SENZA re-ingest, lavorando su ciò che c'è:
      - rimuove le menzioni (:Chunk)-[:MENTIONS]->(:Entity) con score < `min_score`
        (lo score di estrazione è salvato sull'arco → ri-filtrabile a posteriori);
      - rimuove le entità "spazzatura" (nome < `min_entity_len` char, oppure fatto
        solo di cifre/punteggiatura → numerazioni tipo "1.6.1.12.3");
      - rimuove le :Entity rimaste orfane (senza MENTIONS né REL) e i :Content orfani.
    Filtri AGNOSTICI (non dipendono dal dominio). `dry_run` conta soltanto.
    Best-effort: ritorna le statistiche (o {"enabled": False} se il grafo è off)."""
    g = _graph(vector_store_id)
    if g is None:
        return {"enabled": False}

    def _cnt(q, params=None):
        res = g.query(q, params or {})
        return res.result_set[0][0] if res.result_set else 0

    # Le entità le filtro in Python (FalkorDB non ha regex): leggo (id, name).
    ent_rows = g.query("MATCH (e:Entity) RETURN e.id, e.name").result_set or []
    junk_ids = [row[0] for row in ent_rows if _is_junk_entity(row[1], min_entity_len, drop_numeric)]

    out: Dict[str, Any] = {
        "enabled": True,
        "min_score": min_score,
        "min_entity_len": min_entity_len,
        "drop_numeric": drop_numeric,
        "entities_before": len(ent_rows),
        "mentions_before": _cnt("MATCH ()-[r:MENTIONS]->() RETURN count(r)"),
        "weak_mentions": _cnt("MATCH ()-[r:MENTIONS]->() WHERE r.score < $s RETURN count(r)", {"s": min_score}),
        "junk_entities": len(junk_ids),
    }
    if dry_run:
        out["dry_run"] = True
        return out

    g.query("MATCH ()-[r:MENTIONS]->() WHERE r.score < $s DELETE r", {"s": min_score})
    if junk_ids:
        g.query("MATCH (e:Entity) WHERE e.id IN $ids DETACH DELETE e", {"ids": junk_ids})
    g.query("MATCH (e:Entity) WHERE NOT (e)<-[:MENTIONS]-() AND NOT (e)-[:REL]-() DELETE e")
    g.query("MATCH (ct:Content) WHERE NOT (ct)<-[:SAME_CONTENT]-() DELETE ct")

    out["entities_after"] = _cnt("MATCH (e:Entity) RETURN count(e)")
    out["mentions_after"] = _cnt("MATCH ()-[r:MENTIONS]->() RETURN count(r)")
    out["entities_removed"] = out["entities_before"] - out["entities_after"]
    out["mentions_removed"] = out["mentions_before"] - out["mentions_after"]
    return out


# ---------------------------------------------------------------------------
# Scrittura grafo strutturale di un documento
# ---------------------------------------------------------------------------

def write_document_graph(
    vector_store_id: str,
    file_id: str,
    filename: str,
    slug: Optional[str],
    content_hash: Optional[str],
    ingested_at: int,
    chunks: List[Dict[str, Any]],
) -> bool:
    """Scrive l'ossatura documento→sezioni→chunk in FalkorDB.

    `chunks`: lista di dict con chiavi
        chunk_index:int, page:Any, qdrant_point_id:str, text:str, headings:List[str]
        entities:List[{name,type,normalized_name,score}]  (opzionale, M3)

    Idempotente: fa purge dei nodi del file prima di riscrivere. Best-effort:
    ritorna True se ha scritto, False se il grafo è disattivo o c'è stato un
    errore (in tal caso l'errore è loggato, non sollevato).
    """
    g = _graph(vector_store_id)
    if g is None:
        return False

    try:
        # Re-ingest pulito: via i nodi del file, poi riscrivo da zero.
        purge_file_graph(vector_store_id, file_id)

        # 1) Document
        g.query(
            """
            MERGE (d:Document {id:$id})
            SET d.filename=$filename, d.slug=$slug,
                d.content_hash=$content_hash, d.ingested_at=$ts
            """,
            {"id": file_id, "filename": filename, "slug": slug,
             "content_hash": content_hash, "ts": ingested_at},
        )

        # Prepara sezioni + archi + chunk in Python (poche query batch, non una per chunk)
        sections: Dict[str, Dict[str, Any]] = {}   # sec_id -> {title, level, path}
        top_sections: set = set()                  # sezioni di 1° livello (figlie del doc)
        sub_edges: Dict[str, str] = {}             # child_sec_id -> parent_sec_id
        chunk_rows: List[Dict[str, Any]] = []
        chunk_in_section: List[Dict[str, str]] = []  # {chunk, section}
        chunk_in_doc: List[str] = []                 # chunk senza headings
        next_pairs: List[List[str]] = []
        entity_nodes: Dict[str, Dict[str, Any]] = {}  # ent_id -> nodo
        mentions: List[Dict[str, Any]] = []           # {chunk, ent, score}
        content_links: List[Dict[str, str]] = []      # {chunk, hash} (curation)

        prev_chunk_id: Optional[str] = None
        for pos, ch in enumerate(chunks):
            idx = ch.get("chunk_index")
            if idx is None:
                idx = pos
            chunk_id = f"{file_id}::c{idx}"
            headings = [h for h in (ch.get("headings") or []) if h]

            # catena di sezioni dai prefissi dei headings
            leaf_section_id: Optional[str] = None
            parent_sec_id: Optional[str] = None
            for lvl, title in enumerate(headings, start=1):
                path = _SECTION_SEP.join(headings[:lvl])
                sec_id = f"{file_id}::s::{path}"
                sections[sec_id] = {"id": sec_id, "title": title, "level": lvl, "path": path}
                if lvl == 1:
                    top_sections.add(sec_id)
                elif parent_sec_id is not None:
                    sub_edges[sec_id] = parent_sec_id
                parent_sec_id = sec_id
                leaf_section_id = sec_id

            chunk_rows.append({
                "id": chunk_id,
                "idx": idx,
                "page": (ch.get("page_numbers") or [None])[0] if ch.get("page_numbers") else ch.get("page"),
                "point_id": ch.get("qdrant_point_id"),
                "text": ch.get("text") or "",
                "file_id": file_id,
                "filename": filename,
                "body_hash": ch.get("body_hash"),
            })
            if ch.get("body_hash"):
                content_links.append({"chunk": chunk_id, "hash": ch["body_hash"]})
            if leaf_section_id:
                chunk_in_section.append({"chunk": chunk_id, "section": leaf_section_id})
            else:
                chunk_in_doc.append(chunk_id)

            if prev_chunk_id is not None:
                next_pairs.append([prev_chunk_id, chunk_id])
            prev_chunk_id = chunk_id

            # entità del chunk (M3): nodo condiviso per (type, normalized_name)
            for e in (ch.get("entities") or []):
                norm = e.get("normalized_name")
                etype = e.get("type")
                if not norm or not etype:
                    continue
                ent_id = f"{etype}::{norm}"
                entity_nodes[ent_id] = {
                    "id": ent_id,
                    "name": e.get("name", norm),
                    "type": etype,
                    "normalized_name": norm,
                }
                mentions.append({"chunk": chunk_id, "ent": ent_id, "score": float(e.get("score", 0.0))})

        params = {"slug": slug, "fid": file_id}

        # 2) nodi Section
        if sections:
            g.query(
                """
                UNWIND $sections AS s
                MERGE (sec:Section {id:s.id})
                SET sec.title=s.title, sec.level=s.level, sec.path=s.path, sec.slug=$slug
                """,
                {"sections": list(sections.values()), "slug": slug},
            )
        # 3) arco Document -> Section(1° livello)
        if top_sections:
            g.query(
                """
                MATCH (d:Document {id:$fid})
                UNWIND $tops AS sid
                MATCH (sec:Section {id:sid})
                MERGE (d)-[:HAS_SECTION]->(sec)
                """,
                {"fid": file_id, "tops": list(top_sections)},
            )
        # 4) archi Section -> Subsection
        if sub_edges:
            g.query(
                """
                UNWIND $edges AS e
                MATCH (p:Section {id:e.parent}), (c:Section {id:e.child})
                MERGE (p)-[:HAS_SUBSECTION]->(c)
                """,
                {"edges": [{"child": c, "parent": p} for c, p in sub_edges.items()]},
            )
        # 5) nodi Chunk
        if chunk_rows:
            g.query(
                """
                UNWIND $chunks AS ch
                MERGE (c:Chunk {id:ch.id})
                SET c.chunk_index=ch.idx, c.page=ch.page,
                    c.qdrant_point_id=ch.point_id, c.text=ch.text, c.slug=$slug,
                    c.file_id=ch.file_id, c.filename=ch.filename, c.body_hash=ch.body_hash
                """,
                {"chunks": chunk_rows, "slug": slug},
            )
        # 6) arco Section/Document -> Chunk
        if chunk_in_section:
            g.query(
                """
                UNWIND $rows AS r
                MATCH (sec:Section {id:r.section}), (c:Chunk {id:r.chunk})
                MERGE (sec)-[:HAS_CHUNK]->(c)
                """,
                {"rows": chunk_in_section},
            )
        if chunk_in_doc:
            g.query(
                """
                MATCH (d:Document {id:$fid})
                UNWIND $ids AS cid
                MATCH (c:Chunk {id:cid})
                MERGE (d)-[:HAS_CHUNK]->(c)
                """,
                {"fid": file_id, "ids": chunk_in_doc},
            )
        # 7) ordine di lettura :NEXT
        if next_pairs:
            g.query(
                """
                UNWIND $pairs AS p
                MATCH (a:Chunk {id:p[0]}), (b:Chunk {id:p[1]})
                MERGE (a)-[:NEXT]->(b)
                """,
                {"pairs": next_pairs},
            )

        # 8) entità condivise (M3): MERGE → entity resolution cross-documento
        if entity_nodes:
            g.query(
                """
                UNWIND $entities AS e
                MERGE (ent:Entity {id:e.id})
                SET ent.name=e.name, ent.type=e.type, ent.normalized_name=e.normalized_name
                """,
                {"entities": list(entity_nodes.values())},
            )
        # 9) (:Chunk)-[:MENTIONS]->(:Entity)
        if mentions:
            g.query(
                """
                UNWIND $mentions AS m
                MATCH (c:Chunk {id:m.chunk}), (ent:Entity {id:m.ent})
                MERGE (c)-[r:MENTIONS]->(ent)
                SET r.score=m.score
                """,
                {"mentions": mentions},
            )

        # 10) data curation: nodo :Content condiviso per body_hash. Più chunk (di
        # documenti diversi) che puntano allo stesso :Content = stesso contenuto
        # ripetuto → la molteplicità diventa segnale ("boilerplate in N doc")
        # invece di essere buttata. Il conteggio autoritativo per la soppressione
        # sta su Mongo (curation_bodies); qui è la struttura navigabile del grafo.
        if CURATION_GRAPH_LINK and content_links:
            g.query(
                """
                UNWIND $links AS l
                MERGE (ct:Content {id:l.hash})
                WITH ct, l
                MATCH (c:Chunk {id:l.chunk})
                MERGE (c)-[:SAME_CONTENT]->(ct)
                """,
                {"links": content_links},
            )

        logger.info(
            f"📊 graph[{vector_store_id}] doc={file_id}: "
            f"{len(chunk_rows)} chunk, {len(sections)} sezioni, "
            f"{len(entity_nodes)} entità, {len(mentions)} menzioni"
        )
        return True
    except Exception as e:
        logger.warning(f"write_document_graph({vector_store_id}, {file_id}) failed: {e}")
        return False


# ---------------------------------------------------------------------------
# M5 — relazioni tipizzate (GLiNER-relex)
# ---------------------------------------------------------------------------

def write_relations(
    vector_store_id: str, file_id: str, relations: List[Dict[str, Any]]
) -> bool:
    """Scrive archi tipizzati (:Entity)-[:REL {type,score,files}]->(:Entity).

    `relations`: lista di dict {head_name, head_type, head_norm, tail_name,
    tail_type, tail_norm, relation, score} (da models.relex RelexModel.extract).
    Aggrega per (head_id, type, tail_id) tenendo lo score massimo; i nodi :Entity
    usano lo STESSO id `"{type}::{norm}"` della NER → gli archi agganciano gli entity
    node esistenti (MERGE), non ne creano paralleli. La provenienza per-file (`files`)
    consente il purge corretto su re-ingest. Best-effort: errori loggati, non sollevati.
    Idempotente per file: `write_document_graph` ha già fatto il purge del file prima.
    """
    g = _graph(vector_store_id)
    if g is None or not relations:
        return False

    nodes: Dict[str, Dict[str, Any]] = {}
    agg: Dict[tuple, float] = {}
    for r in relations:
        hid = f"{r['head_type']}::{r['head_norm']}"
        tid = f"{r['tail_type']}::{r['tail_norm']}"
        if hid == tid:
            continue
        nodes[hid] = {"id": hid, "name": r["head_name"], "type": r["head_type"],
                      "normalized_name": r["head_norm"]}
        nodes[tid] = {"id": tid, "name": r["tail_name"], "type": r["tail_type"],
                      "normalized_name": r["tail_norm"]}
        key = (hid, r["relation"], tid)
        s = float(r.get("score", 0.0))
        if key not in agg or s > agg[key]:
            agg[key] = s
    if not agg:
        return False

    try:
        g.query(
            """
            UNWIND $nodes AS e
            MERGE (x:Entity {id:e.id})
            SET x.name=e.name, x.type=e.type, x.normalized_name=e.normalized_name
            """,
            {"nodes": list(nodes.values())},
        )
        edges = [{"h": h, "type": ty, "t": t, "score": sc, "fid": file_id}
                 for (h, ty, t), sc in agg.items()]
        g.query(
            """
            UNWIND $edges AS e
            MATCH (h:Entity {id:e.h}), (t:Entity {id:e.t})
            MERGE (h)-[r:REL {type:e.type}]->(t)
            SET r.score = CASE WHEN r.score IS NULL OR e.score > r.score
                               THEN e.score ELSE r.score END,
                r.files = CASE WHEN r.files IS NULL THEN [e.fid]
                               WHEN e.fid IN r.files THEN r.files
                               ELSE r.files + e.fid END
            """,
            {"edges": edges},
        )
        logger.info(f"🔗 graph[{vector_store_id}] doc={file_id}: {len(edges)} relazioni tipizzate")
        return True
    except Exception as e:
        logger.warning(f"write_relations({vector_store_id}, {file_id}) failed: {e}")
        return False


# ---------------------------------------------------------------------------
# M4 — espansione di vicinato (graph-augmented retrieval)
# ---------------------------------------------------------------------------

def expand_neighbors(
    vector_store_id: str,
    seed_point_ids: List[str],
    slugs: Optional[List[str]] = None,
    limit: int = 20,
    df_max: float = 0.5,
    with_next: bool = True,
) -> List[Dict[str, Any]]:
    """Dato un set di chunk trovati da Qdrant (`seed_point_ids` = qdrant_point_id),
    espande il vicinato nel grafo:
      - chunk che condividono :Entity coi seed, **pesando per specificità (IDF)**:
        un'entità rara vale di più, e le "stopword-entity" (document-frequency >
        `df_max`, es. "Banca", "clienti" che compaiono ovunque) vengono escluse —
        altrimenti collegherebbero tutto-a-tutto senza informazione;
      - chunk adiacenti via :NEXT (contesto di lettura).
    Filtra per `slugs` (isolamento per-directory). Ritorna i chunk di vicinato
    (esclusi i seed), col testo già nel grafo. Best-effort → [] se grafo off/errore.
    """
    g = _graph(vector_store_id)
    if g is None or not seed_point_ids:
        return []

    out: Dict[str, Dict[str, Any]] = {}
    slug_clause = "AND rel.slug IN $slugs" if slugs else ""

    # N = chunk totali (nello slug, se filtrato) → serve per la document-frequency
    try:
        n_clause = "WHERE c.slug IN $slugs" if slugs else ""
        n_rows = g.query(f"MATCH (c:Chunk) {n_clause} RETURN count(c)", {"slugs": slugs}).result_set
        total = int(n_rows[0][0]) if n_rows and n_rows[0][0] else 0
    except Exception:
        total = 0
    if total <= 0:
        total = 1  # guard

    # 1) espansione per entità condivise, pesata per IDF (anti stopword-entity)
    try:
        rows = g.query(
            f"""
            UNWIND $seeds AS sid
            MATCH (:Chunk {{qdrant_point_id: sid}})-[:MENTIONS]->(e:Entity)
            WITH DISTINCT e
            MATCH (e)<-[:MENTIONS]-(m:Chunk)
            WITH e, count(m) AS deg
            WHERE toFloat(deg) / $total <= $df_max
            WITH e, log(toFloat($total) / deg) AS idf
            MATCH (e)<-[:MENTIONS]-(rel:Chunk)
            WHERE NOT rel.qdrant_point_id IN $seeds {slug_clause}
            WITH rel, sum(idf) AS weight, count(DISTINCT e) AS shared,
                 collect(DISTINCT e.name) AS via
            RETURN rel.qdrant_point_id AS pid, rel.text AS text,
                   rel.file_id AS file_id, rel.filename AS filename, rel.slug AS slug,
                   weight, shared, via[0..3] AS via
            ORDER BY weight DESC
            LIMIT $limit
            """,
            {"seeds": seed_point_ids, "slugs": slugs, "total": total,
             "df_max": df_max, "limit": limit},
        ).result_set
        for r in rows:
            pid = r[0]
            if not pid:
                continue
            out[pid] = {
                "qdrant_point_id": pid, "text": r[1], "file_id": r[2],
                "filename": r[3], "slug": r[4], "weight": r[5], "shared": r[6],
                "via": r[7], "source": "graph:mentions",
            }
    except Exception as e:
        logger.warning(f"expand_neighbors mentions ({vector_store_id}) failed: {e}")

    # 2) contesto via :NEXT (chunk adiacenti ai seed)
    if with_next:
        try:
            rows = g.query(
                """
                UNWIND $seeds AS sid
                MATCH (:Chunk {qdrant_point_id: sid})-[:NEXT]-(adj:Chunk)
                WHERE NOT adj.qdrant_point_id IN $seeds
                RETURN DISTINCT adj.qdrant_point_id AS pid, adj.text AS text,
                       adj.file_id AS file_id, adj.filename AS filename, adj.slug AS slug
                """,
                {"seeds": seed_point_ids},
            ).result_set
            for r in rows:
                pid = r[0]
                if pid and pid not in out:
                    out[pid] = {
                        "qdrant_point_id": pid, "text": r[1], "file_id": r[2],
                        "filename": r[3], "slug": r[4], "shared": 0, "via": [],
                        "source": "graph:next",
                    }
        except Exception as e:
            logger.warning(f"expand_neighbors next ({vector_store_id}) failed: {e}")

    return list(out.values())


# --- Export per la visualizzazione (knowledge-graph viz, force-graph) ---

_GRAPH_DISPLAY = {  # label -> proprietà usata come "name" del nodo nel viewer
    "Document": "filename",
    "Section": "title",
    "Entity": "name",
}


def _node_name(label: str, props: Dict[str, Any]) -> str:
    if label == "Chunk":
        t = (props.get("text") or "").strip().replace("\n", " ")
        return (t[:70] + "…") if len(t) > 70 else (t or f"chunk {props.get('idx', '')}")
    if label == "Content":
        return f"content {(props.get('hash') or '')[:8]}"
    key = _GRAPH_DISPLAY.get(label)
    if key and props.get(key):
        return props[key]
    return props.get("name") or props.get("title") or props.get("filename") or label


def _slim_props(props: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(props or {})
    # tronca il testo dei chunk (può essere ~2k char) per non gonfiare il payload
    if isinstance(out.get("text"), str) and len(out["text"]) > 300:
        out["text"] = out["text"][:300] + "…"
    return out


def export_graph(vector_store_id: str, limit: int = 2000) -> Dict[str, Any]:
    """Esporta il grafo del vector store come {nodes, links, metadata} per il viewer
    force-graph (`links`, non `edges`). Best-effort: grafo vuoto se FalkorDB è giù.
    `limit` = numero massimo di nodi; gli archi sono tenuti solo tra i nodi esportati."""
    import time
    g = _graph(vector_store_id)
    if g is None:
        return {"nodes": [], "links": [],
                "metadata": {"node_count": 0, "edge_count": 0, "graph_enabled": False}}
    t0 = time.time()
    nodes: List[Dict[str, Any]] = []
    kept: set = set()
    try:
        nres = g.query(
            "MATCH (n) RETURN id(n), labels(n), properties(n) LIMIT $lim",
            {"lim": int(limit)},
        ).result_set
        for nid, labels, props in nres:
            label = labels[0] if labels else "Node"
            props = props or {}
            nodes.append({"id": str(nid), "label": label,
                          "name": _node_name(label, props), "properties": _slim_props(props)})
            kept.add(nid)
    except Exception as e:
        logger.warning(f"export_graph nodes ({vector_store_id}) failed: {e}")
        return {"nodes": [], "links": [], "metadata": {"node_count": 0, "edge_count": 0, "error": str(e)}}

    links: List[Dict[str, Any]] = []
    try:
        eres = g.query("MATCH (a)-[r]->(b) RETURN id(a), id(b), type(r)").result_set
        for a, b, rtype in eres:
            if a in kept and b in kept:
                links.append({"source": str(a), "target": str(b), "type": rtype})
    except Exception as e:
        logger.warning(f"export_graph edges ({vector_store_id}) failed: {e}")

    return {"nodes": nodes, "links": links, "metadata": {
        "node_count": len(nodes), "edge_count": len(links),
        "query_time_ms": round((time.time() - t0) * 1000, 1),
        "truncated": len(nodes) >= int(limit),
    }}


def subgraph_for_points(
    vector_store_id: str,
    point_ids: List[str],
    include_relations: bool = True,
    include_next: bool = False,
) -> Dict[str, Any]:
    """Ricostruisce il sottografo attorno a un insieme di chunk (per `qdrant_point_id`):
    i chunk seed + il loro Documento di provenienza + le Entità menzionate + (opz) le
    relazioni tipizzate :REL tra quelle entità e i chunk adiacenti :NEXT. È il backing
    della "search-as-graph": dai risultati di una ricerca si vede il dato ricostruito.
    Ritorna {nodes, links, metadata, seed_ids}. Best-effort → vuoto se grafo off/errore."""
    import time
    empty = {"nodes": [], "links": [], "metadata": {"node_count": 0, "edge_count": 0}, "seed_ids": []}
    g = _graph(vector_store_id)
    if g is None or not point_ids:
        return empty
    t0 = time.time()
    nodes: Dict[str, Dict[str, Any]] = {}
    links: List[Dict[str, Any]] = []
    seen: set = set()

    def add_node(nid: Any, labels: Any, props: Any) -> str:
        sid = str(nid)
        if sid not in nodes:
            label = labels[0] if labels else "Node"
            props = props or {}
            nodes[sid] = {"id": sid, "label": label,
                          "name": _node_name(label, props), "properties": _slim_props(props)}
        return sid

    def add_link(a: Any, b: Any, t: str) -> None:
        k = (str(a), str(b), t)
        if k not in seen:
            seen.add(k)
            links.append({"source": str(a), "target": str(b), "type": t})

    try:
        seed_ids: List[str] = []
        for nid, labels, props in g.query(
            "MATCH (c:Chunk) WHERE c.qdrant_point_id IN $p RETURN id(c), labels(c), properties(c)",
            {"p": point_ids},
        ).result_set:
            seed_ids.append(add_node(nid, labels, props))
        if not seed_ids:
            return empty

        # Documento di provenienza (collega diretto Document→Chunk, saltando la Section)
        for cid, did, dl, dp in g.query(
            "MATCH (c:Chunk) WHERE c.qdrant_point_id IN $p "
            "MATCH (d:Document {id: c.file_id}) RETURN id(c), id(d), labels(d), properties(d)",
            {"p": point_ids},
        ).result_set:
            add_node(did, dl, dp)
            add_link(did, cid, "HAS_CHUNK")

        # Entità menzionate dai chunk seed
        ent_ids: set = set()
        for cid, eid, el, ep in g.query(
            "MATCH (c:Chunk) WHERE c.qdrant_point_id IN $p "
            "MATCH (c)-[:MENTIONS]->(e:Entity) RETURN id(c), id(e), labels(e), properties(e)",
            {"p": point_ids},
        ).result_set:
            add_node(eid, el, ep)
            add_link(cid, eid, "MENTIONS")
            ent_ids.add(eid)

        # Relazioni tipizzate tra le entità incluse (mostra il TIPO semantico, es. "emesso da")
        if include_relations and ent_ids:
            for a, b, rtype in g.query(
                "MATCH (e1:Entity)-[r:REL]->(e2:Entity) WHERE id(e1) IN $ids AND id(e2) IN $ids "
                "RETURN id(e1), id(e2), r.type",
                {"ids": list(ent_ids)},
            ).result_set:
                add_link(a, b, rtype or "REL")

        # Chunk adiacenti via :NEXT (contesto di lettura)
        if include_next:
            for cid, aid, al, ap in g.query(
                "MATCH (c:Chunk) WHERE c.qdrant_point_id IN $p "
                "MATCH (c)-[:NEXT]-(a:Chunk) RETURN id(c), id(a), labels(a), properties(a)",
                {"p": point_ids},
            ).result_set:
                add_node(aid, al, ap)
                add_link(cid, aid, "NEXT")
    except Exception as e:
        logger.warning(f"subgraph_for_points ({vector_store_id}) failed: {e}")

    return {"nodes": list(nodes.values()), "links": links, "seed_ids": seed_ids,
            "metadata": {"node_count": len(nodes), "edge_count": len(links),
                         "query_time_ms": round((time.time() - t0) * 1000, 1)}}
