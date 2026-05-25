"""
Sophia Vector — Core configuration.

Tutte le env applicative hanno prefisso SOPHIA_VECTOR_ (es.
SOPHIA_VECTOR_QDRANT_URL). I default puntano a localhost per lo sviluppo;
in produzione il docker-compose passa le env esplicite.

`utils/settings.py` espone le costanti derivate da qui, così il codice
esistente che fa `from utils.settings import QDRANT_URL` continua a funzionare.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- App ---
    APP_NAME: str = "Sophia Vector"
    APP_VERSION: str = "0.2.0-alpha"
    DEBUG: bool = False

    # --- Auth / sicurezza ---
    # API_KEY: se valorizzata, gli endpoint /v1/* richiedono Authorization: Bearer.
    # CORS_ORIGINS: csv di origin permesse; vuoto = "*".
    # SECRET_KEY: master key per cifrare i secret delle ingestion sources.
    API_KEY: str = ""
    CORS_ORIGINS: str = ""
    SECRET_KEY: str = ""

    # --- MongoDB (URI completo con nome db) ---
    # Dev/default: sophia_vector. In prod il db storico si chiama "sophia":
    # al deploy o si migra o si punta l'URI al nome esistente.
    MONGODB_URI: str = "mongodb://localhost:27017/sophia_vector"

    # --- Qdrant ---
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str = ""

    # --- Docling (parser) ---
    DOCLING_URL: str = "http://localhost:5001"

    # --- Embeddings + rerank (BGE-M3 service, espone /v1/embeddings e /v1/rerank) ---
    EMBEDDINGS_URL: str = "http://localhost:8004"

    # --- FalkorDB (knowledge graph; layer ADDITIVO sopra Qdrant) ---
    # GRAPH_ENABLED=False disattiva del tutto la scrittura grafo (la pipeline
    # Qdrant resta identica). La scrittura è comunque best-effort: se FalkorDB
    # è giù, l'ingestion non fallisce. In prod overridare FALKOR_PASSWORD.
    GRAPH_ENABLED: bool = True
    FALKOR_HOST: str = "localhost"
    FALKOR_PORT: int = 6379
    FALKOR_PASSWORD: str = "falkordb"
    # Prefisso namespace per i nomi grafo. Una sola istanza FalkorDB può ospitare
    # più progetti: ogni grafo è una chiave Redis separata. Settando un prefisso
    # diverso per progetto (es. "sv:") si evitano collisioni di nomi. Vuoto = il
    # nome grafo è esattamente il vector_store_id.
    FALKOR_GRAPH_PREFIX: str = ""

    # --- Entity extraction (M3 grafo: GLiNER zero-shot + regex, niente LLM) ---
    # GLINER_ENABLED=False → solo regex (IBAN/CF/P.IVA). Le label sono CSV e
    # vengono passate a GLiNER a runtime (zero-shot, dominio bancario IT).
    GLINER_ENABLED: bool = True
    GLINER_MODEL: str = "urchade/gliner_multi-v2.1"
    GLINER_THRESHOLD: float = 0.5
    GLINER_LABELS: str = "organizzazione,persona,normativa,data,importo monetario,luogo,prodotto finanziario"
    # Device del modello GLiNER: "cpu" | "cuda" | "cuda:N" | "auto" (GPU se c'è,
    # altrimenti CPU). Default CPU: il worker gira spesso sullo stesso host dove la
    # GPU è già presa da vLLM/embeddings/parser, e GLiNER è piccolo (~50ms/chunk su
    # CPU). Su un host con GPU libera il dev fa opt-in con "cuda". Se CUDA non è
    # disponibile il caricamento ricade su CPU (best-effort, non rompe l'ingestion).
    GLINER_DEVICE: str = "cpu"

    # --- Relation extraction (M5 grafo: GLiNER-relex, archi TIPIZZATI) ---
    # Layer ADDITIVO sopra le entità: oltre a "compaiono insieme", estrae relazioni
    # tipizzate zero-shot (es. "Decreto —ai sensi di→ D.lgs. 231/2001", "Doc —pubblicato
    # da→ Org") in una passata joint NER+RE. Stesso filone di GLiNER (stessa lib, multi-
    # lingue per l'IT), pattern lazy/device identico (riusa GLINER_DEVICE). Default OFF:
    # è un 2° modello in RAM + ~0.2s/chunk → opt-in. Le label di relazione sono CSV,
    # zero-shot, da tarare sul dominio. Best-effort: un guasto qui non rompe l'ingestion.
    RELATIONS_ENABLED: bool = False
    RELATIONS_MODEL: str = "knowledgator/gliner-relex-multi-v1.0"
    RELATIONS_ENTITY_THRESHOLD: float = 0.5
    RELATIONS_THRESHOLD: float = 0.45
    RELATIONS_ADJACENCY_THRESHOLD: float = 0.55
    # Default snello: solo relazioni SPECIFICHE ad alto valore. Le generiche
    # ("si applica a", "riguarda") sono state tolte dal default perché dominavano il
    # volume con poco segnale — chi le vuole le aggiunge via schema (globale o
    # per-vector-store). Restano comunque agnostiche: sono solo stringhe zero-shot.
    RELATIONS_LABELS: str = "pubblicato da,pubblicato il,emesso da,ai sensi di,modifica,sostituisce,fa riferimento a"
    # Filtri di igiene del segnale (agnostici, non dominio-specifici): scarta gli
    # estremi troppo lunghi (frasi, non entità) e le relazioni tra due entità
    # entrambe non tipizzate ("other"→"other", tipico rumore da tabelle/figure).
    RELATIONS_MAX_ENTITY_WORDS: int = 8
    RELATIONS_DROP_OTHER_TO_OTHER: bool = True

    # --- Storage su disco ---
    FILES_STORAGE: str = "/app/storage/files"
    DOCUMENTS_STORAGE: str = "/app/storage/documents"
    MAX_FILE_SIZE_MB: int = 512

    # --- Chunking / indexing defaults ---
    DEFAULT_CHUNK_SIZE: int = 1024
    DEFAULT_CHUNK_OVERLAP: int = 128
    DEFAULT_EMBEDDING_DIMENSION: int = 1024
    DEFAULT_POINTS_BATCH_SIZE: int = 64

    # --- Parser Docling (tuning; ex env legacy os.getenv senza prefisso) ---
    PARSER_MODEL_TOKENIZER: str = "BAAI/bge-m3"
    # Chunk più piccoli = retrieval più preciso; il contesto perso viene
    # recuperato a query time dal grafo (:NEXT). Vedi M4 graph-augmented retrieval.
    PARSER_MODEL_MAX_TOKENS: int = 512
    # OCR acceso di default: i PDF scansionati e le immagini vengono letti.
    # Si abbina a force_ocr=False (in utils/docling.py): l'OCR scatta SOLO sulle
    # pagine/immagini senza text layer — i PDF nativi non vengono ri-OCR-ati
    # (force_ocr=True farebbe "solo OCR" su tutto = lento e peggiore sul testo
    # nativo). Disattivabile con SOPHIA_VECTOR_PARSER_USE_OCR=false.
    PARSER_USE_OCR: bool = True
    PARSER_PICTURE_DESCRIPTION: bool = False
    # "fast" e non "accurate": su tabelle a celle larghe (es. circolari
    # "categoria | descrizione") TableFormer accurate inventa griglie con
    # spanning patologico (es. 318x12 per una tabella a 2 colonne) e il
    # serializer le espande → 30KB di testo diventano 3MB di markdown / ~9.7M
    # token, e il chunking esplode (ReadTimeout in prod). "fast" rileva la
    # struttura senza l'esplosione; i triplets restano. Vedi issue docling #3428.
    PARSER_TABLE_MODE: str = "fast"
    PARSER_TABLE_CELL_MATCHING: bool = False
    PARSER_PDF_BACKEND: str = "docling_parse"
    # Timeout per-documento inviato a Docling. Default alto (prod); le istanze
    # Docling con un massimo più basso (es. dev = 1800s) vanno sotto quel tetto.
    PARSER_MAX_WAIT_SECONDS: int = 36000

    # --- ASR (trascrizione audio/video; faster-whisper su CPU) ---
    # Stesso pattern di GLiNER: il modello si carica LAZY al primo file audio/video
    # e resta caldo nel processo del vector worker (nessun costo se non arriva
    # audio). ASR_ENABLED=False → audio/video escono dalla whitelist (rifiutati a
    # monte invece di fallire). I limiti di durata sono lo "sweet spot" Sophia: il
    # dev li alza via env per file più lunghi. Modello: tiny|base|small|medium|large-v3.
    ASR_ENABLED: bool = True
    ASR_MODEL: str = "small"
    ASR_LANGUAGE: str = "it"
    # Device Whisper: "cpu" | "cuda" | "cuda:N" | "auto". Stessa logica di GLINER_DEVICE
    # (default CPU, opt-in GPU). faster-whisper/CTranslate2 fa la sua detection con
    # "auto". COMPUTE_TYPE "int8" va bene su entrambi; su GPU si può alzare a
    # "float16"/"int8_float16" per più qualità.
    ASR_DEVICE: str = "cpu"
    ASR_COMPUTE_TYPE: str = "int8"
    ASR_MAX_AUDIO_MINUTES: int = 60
    ASR_MAX_VIDEO_MINUTES: int = 30

    # --- Data curation (dedup del CONTENUTO, non dei dati) ---
    # Tesi: ingerire MEGLIO, non di più. Docling contestualizza il testo del chunk
    # con un prefisso heading "inbody" (titolo-doc › sezione): lo stesso disclaimer
    # sotto sezioni/documenti diversi ha quindi un `text` diverso. Il dedup vero è
    # sul BODY (testo senza il prefisso heading), che ricostruiamo da `headings`.
    # Per ogni collection teniamo `body_hash → in quanti documenti compare`:
    #   CURATION_ENABLED          → master switch del layer (off = pipeline invariata).
    #   CURATION_GRAPH_LINK       → collega nel grafo i chunk con stesso body
    #                               (:Chunk)-[:SAME_CONTENT]->(:Content {hash, doc_count}):
    #                               la molteplicità diventa segnale (provenienza +
    #                               "boilerplate in N doc") invece di essere buttata.
    #   CURATION_BOILERPLATE_*    → a search-time un chunk il cui body compare in oltre
    #                               RATIO dei documenti della collection (e in almeno
    #                               MIN_DOCS) è boilerplate → escluso/deprioritizzato.
    #                               Migliora le risposte: disclaimer/intestazioni
    #                               ripetute smettono di saturare i top-K.
    CURATION_ENABLED: bool = True
    CURATION_GRAPH_LINK: bool = True
    CURATION_BOILERPLATE_RATIO: float = 0.5
    CURATION_BOILERPLATE_MIN_DOCS: int = 5

    # --- Ingestion worker (tuning; ex env legacy os.getenv senza prefisso) ---
    INGEST_BATCH_SIZE: int = 64
    INGEST_MAX_CONCURRENT_JOBS: int = 1
    INGEST_WAIT_TIME_JOBS: float = 3.0
    SHAREPOINT_POLL_INTERVAL: float = 5.0

    # --- Scheduler (cron interno per le sync; sostituisce il cron di sistema) ---
    # Lo scheduler worker chiama l'endpoint di sync via HTTP (riusa l'overlap guard
    # in-app). In dev il backend è :8100, in prod :8003 → override in prod.
    INTERNAL_API_URL: str = "http://127.0.0.1:8100"
    SCHEDULER_POLL_INTERVAL: float = 30.0

    model_config = {
        "env_prefix": "SOPHIA_VECTOR_",
        "env_file": ".env",
        "extra": "ignore",
    }


settings = Settings()
