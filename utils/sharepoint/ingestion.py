import asyncio
import os
import re
import time
import requests
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse
from pydantic import BaseModel, Field, field_validator
from fastapi import HTTPException
from enum import Enum

from utils import get_timestamp
from utils.database import db
from utils.docling import PARSER_SUPPORTED_EXTENSIONS
from utils.filesystem import store_file_on_disk, delete_file_from_disk
from utils.logger import get_logger
from utils.qdrant import delete_qdrant_points
from utils.settings import MAX_FILE_SIZE
from utils.exclusions import is_excluded

logger = get_logger(__name__)

# Sharepoint Configs Data
AZURE_SITE_URL = os.getenv("AZURE_SITE_URL", "")
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")

# create collections for jobs async management
ingestion_jobs = db["ingestion_jobs"]
sharepoint_jobs = db["sharepoint_jobs"]

# Attributi gestiti internamente dalla pipeline SharePoint: l'utente non li può
# passare via config e non vengono usati per il matching cross-job in mongo.
RESERVED_ATTR_KEYS = frozenset({"sharepoint_file_modified", "sharepoint_file_id"})

# Retry policy per Graph API (rate limit / throttling)
GRAPH_MAX_RETRIES = 5
GRAPH_RETRY_BASE_SECONDS = 1.0
GRAPH_RETRY_MAX_SECONDS = 30.0
GRAPH_DOWNLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB


# ================== MODELLI PYDANTIC ==================

class IngestStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class IngestResponse(BaseModel):
    ingestion_id: str
    site_id: str
    status: IngestStatus


class SkippedFile(BaseModel):
    name: str
    reason: str


class IngestStatusResponse(BaseModel):
    status: IngestStatus
    error: Optional[str] = None
    site_id: str

    created_at: int
    updated_at: int

    folders_processed: int
    total_files: int
    processed_files: int
    skipped_files: int
    files_failed: int
    skipped_files_lists: List[SkippedFile] = []
    total_size: int


_TENANT_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# dominio Azure (es. contoso.onmicrosoft.com): label alfanumeriche separate da punto,
# niente slash/spazi/'..'/'@' → impedisce path injection nell'URL del token.
_TENANT_DOMAIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)+")


class SharePointAuth(BaseModel):
    """Configurazione per autenticazione SharePoint via Graph API"""
    site_url: str
    client_id: str
    client_secret: str
    tenant_id: str  # Obbligatorio per Graph API

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, v: str) -> str:
        # tenant_id finisce nel PATH del token URL (login.microsoftonline.com/{tenant}/...):
        # deve essere un GUID o un dominio Azure, MAI contenere '/', '..', '@' o spazi
        # → anti-SSRF / path injection.
        v = (v or "").strip()
        if not _TENANT_GUID_RE.fullmatch(v) and not _TENANT_DOMAIN_RE.fullmatch(v):
            raise ValueError("tenant_id deve essere un GUID o un dominio Azure (es. contoso.onmicrosoft.com)")
        return v

    @field_validator("site_url")
    @classmethod
    def _validate_site_url(cls, v: str) -> str:
        # site_url guida le chiamate Graph: enforce https + host *.sharepoint.com così il
        # backend non può essere puntato a host arbitrari (SSRF: metadata cloud, servizi interni).
        v = (v or "").strip()
        parsed = urlparse(v)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "sharepoint.com" or host.endswith(".sharepoint.com")):
            raise ValueError("site_url deve essere un URL https su un host *.sharepoint.com")
        return v


class FolderConfig(BaseModel):
    """Configurazione per una singola folder da indicizzare"""
    sharepoint_id: str  # ID della cartella in SharePoint (formato Graph API)
    name: Optional[str] = None  # nome leggibile (solo display UI; non usato dall'ingest)
    recursive: bool = True  # Se true, indicizza anche sottocartelle


class Rule(BaseModel):
    """
    Regola di filtro per i file SharePoint.

    type:
        - "pattern": lista di sottostringhe da cercare nel nome file (case-insensitive).
          Esempio: ".pdf, .docx" includerà tutti i file che contengono ".pdf" o ".docx" nel nome.
        - "regex": espressione regolare Python applicata al nome del file con `re.match`,
          quindi il pattern viene valutato dall’inizio del nome file.
          Esempio: r"^ORG-STGN.*\\.pdf$" includerà solo i PDF che iniziano con "ORG-STGN".
    """
    type: str = Field(
        description='Tipo di regola: "pattern" o "regex". '
                    '"pattern" usa una lista di sottostringhe; '
                    '"regex" usa un\'espressione regolare Python su tutto il nome file.'
    )
    value: str = Field(
        description=(
            'Se type="pattern": lista di pattern separati da virgola (es: ".pdf,.docx"). '
            'Se type="regex": espressione regolare Python applicata con re.match() '
            'al nome completo del file (es: r"^ORG-STGN.*\\\\.pdf$").'
        )
    )


class IngestConfig(BaseModel):
    """
    Configurazione completa per un job di ingest da SharePoint.

    - include_rules: se valorizzato, un file viene considerato solo se soddisfa
      **almeno una** delle regole di include.
    - exclude_rules: se valorizzato, un file viene scartato se soddisfa
      **qualunque** regola di exclude.

    Le regole vengono applicate in quest’ordine:
      1. include_rules (se presenti)
      2. exclude_rules
      3. controllo su job già esistenti:
         - file già indicizzato con stessa data di modifica → SKIP
         - file con job in PENDING/PROCESSING → SKIP
         - file con modifica diversa → reindex (cancella vecchio e ricrea job)
    """
    vector_store_id: str = Field(
        description="ID del vector store (collezione Qdrant) in cui indicizzare i documenti."
    )
    source_id: Optional[str] = Field(
        default=None,
        description=(
            "ID della ingestion source (credenziali SharePoint). Se assente, "
            "si usano le credenziali globali da env (comportamento legacy)."
        ),
    )
    folders: List[FolderConfig] = Field(
        description="Lista di cartelle SharePoint da indicizzare."
    )
    include_rules: Optional[List[Rule]] = Field(
        default_factory=list,
        alias="include_rules",
        description=(
            "Regole di include: se valorizzate, un file viene elaborato solo se "
            "rispetta almeno una delle regole."
        ),
    )
    exclude_rules: Optional[List[Rule]] = Field(
        default_factory=list,
        alias="exclude_rules",
        description=(
            "Regole di exclude: se valorizzate, un file viene scartato se rispetta "
            "qualunque regola."
        ),
    )
    attributes: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Attributi custom che verranno salvati su ogni job di ingest e propagati "
            "ai punti in Qdrant (es: tenant_id, business_unit, ecc.)."
        ),
    )


# ================== GRAPH API CLIENT ==================

class GraphAPIClient:
    """Client per interagire con Microsoft Graph API"""

    def __init__(self, auth: SharePointAuth):
        self.auth = auth
        self.token = None
        self.headers = None
        self.site_id = None
        self.drives = {}  # Cache dei drives

    def get_access_token(self) -> str:
        """Ottiene il token OAuth2 da Azure AD"""
        token_url = f"https://login.microsoftonline.com/{self.auth.tenant_id}/oauth2/v2.0/token"

        token_data = {
            'grant_type': 'client_credentials',
            'client_id': self.auth.client_id,
            'client_secret': self.auth.client_secret,
            'scope': 'https://graph.microsoft.com/.default'
        }

        logger.info("Requesting access token from Azure AD...")

        response = requests.post(token_url, data=token_data)

        if response.status_code == 200:
            self.token = response.json()['access_token']
            self.headers = {
                'Authorization': f'Bearer {self.token}',
                'Accept': 'application/json'
            }
            logger.info("✓ Access token obtained successfully")
            return self.token
        else:
            # Non esporre response.text al client né nei log: il body di errore del
            # token endpoint può contenere dettagli AADSTS/correlation. Status + msg generico.
            logger.error(f"Azure AD token request failed: status {response.status_code}")
            raise HTTPException(
                status_code=401,
                detail="Failed to authenticate with Azure AD (verifica le credenziali della source)",
            )

    def _retry_delay(self, attempt: int, response: Optional[requests.Response]) -> float:
        """Calcola il delay per il prossimo retry, onorando Retry-After se presente."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), GRAPH_RETRY_MAX_SECONDS)
                except ValueError:
                    pass
        return min(GRAPH_RETRY_BASE_SECONDS * (2 ** attempt), GRAPH_RETRY_MAX_SECONDS)

    def make_graph_request(
            self,
            url: str,
            method: str = "GET",
            expect_json: bool = True,
    ):
        """Esegue una richiesta autenticata a Graph API con retry su 401/429/5xx/timeout.

        Per il download di file binari usare `download_file_to_path` (streaming),
        non questa funzione: tenere tutto in memoria su file grandi causa OOM.
        """
        if not self.headers:
            self.get_access_token()

        last_response: Optional[requests.Response] = None

        for attempt in range(GRAPH_MAX_RETRIES):
            try:
                response = requests.request(method, url, headers=self.headers, timeout=60)
            except requests.exceptions.RequestException as e:
                logger.warning(f"Graph request error (attempt {attempt + 1}): {e}")
                time.sleep(self._retry_delay(attempt, None))
                continue

            last_response = response

            if response.status_code == 401:
                logger.info("Token expired, refreshing...")
                self.get_access_token()
                continue

            if response.status_code in (429, 503):
                delay = self._retry_delay(attempt, response)
                logger.warning(
                    f"Graph API throttled (status {response.status_code}), "
                    f"retrying in {delay:.1f}s (attempt {attempt + 1}/{GRAPH_MAX_RETRIES})"
                )
                time.sleep(delay)
                continue

            if 500 <= response.status_code < 600:
                delay = self._retry_delay(attempt, response)
                logger.warning(
                    f"Graph API server error (status {response.status_code}), "
                    f"retrying in {delay:.1f}s (attempt {attempt + 1}/{GRAPH_MAX_RETRIES})"
                )
                time.sleep(delay)
                continue

            if 200 <= response.status_code < 300:
                return response.json() if expect_json else response.content

            logger.error(
                f"Graph API request failed: {response.status_code} | "
                f"URL: {url} | Response: {response.text[:500]}"
            )
            return None

        status = last_response.status_code if last_response is not None else "n/a"
        logger.error(f"Graph API request exhausted retries (last status: {status}) | URL: {url}")
        return None

    def download_file_to_path(self, drive_id: str, item_id: str, output_path: str) -> int:
        """Scarica un file Graph in streaming verso `output_path`.

        Non tiene il contenuto in memoria. Ritorna il numero di byte scritti.
        Solleva HTTPException se il download fallisce dopo i retry.
        """
        if not self.site_id:
            self.connect_to_site()
        if not self.headers:
            self.get_access_token()

        url = (
            f"https://graph.microsoft.com/v1.0/sites/{self.site_id}"
            f"/drives/{drive_id}/items/{item_id}/content"
        )

        for attempt in range(GRAPH_MAX_RETRIES):
            try:
                with requests.get(url, headers=self.headers, stream=True, timeout=120) as response:
                    if response.status_code == 401:
                        logger.info("Token expired during download, refreshing...")
                        self.get_access_token()
                        continue

                    if response.status_code in (429, 503) or 500 <= response.status_code < 600:
                        delay = self._retry_delay(attempt, response)
                        logger.warning(
                            f"Download throttled/error (status {response.status_code}), "
                            f"retrying in {delay:.1f}s (attempt {attempt + 1}/{GRAPH_MAX_RETRIES})"
                        )
                        time.sleep(delay)
                        continue

                    response.raise_for_status()

                    bytes_written = 0
                    with open(output_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=GRAPH_DOWNLOAD_CHUNK_BYTES):
                            if chunk:
                                f.write(chunk)
                                bytes_written += len(chunk)
                    return bytes_written

            except requests.exceptions.RequestException as e:
                logger.warning(f"Download request error (attempt {attempt + 1}): {e}")
                # rimuovi il file parziale se è stato creato
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except OSError:
                        pass
                time.sleep(self._retry_delay(attempt, None))

        raise HTTPException(
            status_code=500,
            detail=f"Failed to download from SharePoint after {GRAPH_MAX_RETRIES} attempts "
                   f"(drive={drive_id}, item={item_id})",
        )

    def connect_to_site(self) -> str:
        """Connette al sito SharePoint e ottiene l'ID"""
        # Parsing dell'URL del sito
        parts = self.auth.site_url.replace("https://", "").split("/")
        hostname = parts[0]
        site_path = "/".join(parts[1:])

        graph_url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:/{site_path}"

        logger.info(f"Connecting to site: {self.auth.site_url}")

        result = self.make_graph_request(graph_url)

        if result:
            self.site_id = result.get('id')
            logger.info(f"✓ Connected to site: {result.get('displayName', 'Unknown')}")
            logger.info(f"  Site ID: {self.site_id}")
            return self.site_id
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Failed to connect to SharePoint site: {self.auth.site_url}"
            )

    def get_drives(self) -> List[Dict[str, Any]]:
        """Ottiene la lista dei document libraries (drives)"""
        if not self.site_id:
            self.connect_to_site()

        url = f"https://graph.microsoft.com/v1.0/sites/{self.site_id}/drives"

        logger.info("Fetching document libraries...")

        result = self.make_graph_request(url)

        if result:
            drives = result.get('value', [])
            logger.info(f"✓ Found {len(drives)} document libraries")

            # Cache drives per lookup veloce
            for drive in drives:
                self.drives[drive['id']] = drive
                logger.info(f"  - {drive.get('name', 'Unknown')} (ID: {drive.get('id', 'Unknown')})")

            return drives

        return []

    def get_folder_info(self, folder_id: str) -> Optional[Dict[str, Any]]:
        """Ottiene informazioni su una cartella tramite il suo ID"""
        if not self.drives:
            self.get_drives()

        # Prova in ogni drive finché non trova la cartella
        for drive_id, drive_info in self.drives.items():
            url = f"https://graph.microsoft.com/v1.0/sites/{self.site_id}/drives/{drive_id}/items/{folder_id}"

            result = self.make_graph_request(url)

            if result:
                logger.info(f"✓ Found folder '{result.get('name')}' in drive '{drive_info.get('name')}'")
                # Aggiungi drive_id al risultato per uso futuro
                result['_drive_id'] = drive_id
                return result

        logger.error(f"Folder {folder_id} not found in any drive")
        return None

    def iter_folder_files(
            self,
            drive_id: str,
            folder_id: str,
            recursive: bool = False,
            depth: int = 0,
            max_depth: int = 100,
    ):
        """Itera sui file di una cartella (ricorsivo opzionale) e li yielda uno per uno."""
        if depth > max_depth:
            logger.warning(f"Max recursion depth {max_depth} reached")
            return

        indent = "  " * depth

        url = f"https://graph.microsoft.com/v1.0/sites/{self.site_id}/drives/{drive_id}/items/{folder_id}/children"

        while url:
            result = self.make_graph_request(url)
            if not result:
                break

            items = result.get("value", [])
            for item in items:
                item_name = item.get("name", "")
                item_id = item.get("id", "")
                is_folder = "folder" in item

                if is_folder:
                    logger.info(f"{indent}📁 Found subfolder: {item_name}")
                    if recursive:
                        # ricorsione: yielda i file delle sottocartelle
                        yield from self.iter_folder_files(
                            drive_id=drive_id,
                            folder_id=item_id,
                            recursive=recursive,
                            depth=depth + 1,
                            max_depth=max_depth,
                        )
                else:
                    file_info = {
                        "id": item_id,
                        "name": item_name,
                        "size": item.get("size", 0),
                        "created": item.get("createdDateTime", ""),
                        "modified": item.get("lastModifiedDateTime", ""),
                        "web_url": item.get("webUrl", ""),
                        "mime_type": item.get("file", {}).get("mimeType", ""),
                        "_drive_id": drive_id,
                        "_parent_id": folder_id,
                        "_depth": depth,
                    }
                    logger.info(f"{indent}📄 Found file: {item_name} ({file_info['size']:,} bytes)")
                    yield file_info
            url = result.get("@odata.nextLink")

    def list_children(self, drive_id: str, folder_id: Optional[str] = None) -> Dict[str, Any]:
        """Lista i figli di un drive/cartella separati in cartelle e file — per
        il browsing UI. Se folder_id è None parte dalla root del drive.
        Ritorna {folders: [...], files: [...]}.
        """
        if not self.site_id:
            self.connect_to_site()

        if folder_id:
            url = f"https://graph.microsoft.com/v1.0/sites/{self.site_id}/drives/{drive_id}/items/{folder_id}/children"
        else:
            url = f"https://graph.microsoft.com/v1.0/sites/{self.site_id}/drives/{drive_id}/root/children"

        folders: List[Dict[str, Any]] = []
        files: List[Dict[str, Any]] = []
        while url:
            result = self.make_graph_request(url)
            if not result:
                break
            for item in result.get("value", []):
                if "folder" in item:
                    folders.append({
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "child_count": item.get("folder", {}).get("childCount", 0),
                    })
                elif "file" in item:
                    files.append({
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "size": item.get("size", 0),
                    })
            url = result.get("@odata.nextLink")
        return {"folders": folders, "files": files}


# ================== AUTH RESOLUTION ==================

def default_env_auth() -> SharePointAuth:
    """Credenziali SharePoint globali da env (fallback legacy). Usato solo quando un
    job NON ha source_id: logga un warning perché in un setup multi-source significa
    autenticare con le credenziali globali invece che con quelle della source."""
    logger.warning("SharePoint: uso credenziali legacy da env AZURE_* (job senza source_id)")
    return SharePointAuth(
        site_url=AZURE_SITE_URL,
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
    )


def resolve_source_auth(source_id: str) -> SharePointAuth:
    """Costruisce le credenziali a partire da una ingestion source su MongoDB.

    Il client_secret è cifrato at-rest e viene decifrato qui.
    """
    from utils.crypto import decrypt_secret

    doc = db["ingestion_sources"].find_one({"_id": source_id})
    if not doc:
        raise ValueError(f"Ingestion source '{source_id}' not found")

    cfg = doc.get("config") or {}
    return SharePointAuth(
        site_url=cfg.get("site_url", ""),
        tenant_id=cfg.get("tenant_id", ""),
        client_id=cfg.get("client_id", ""),
        client_secret=decrypt_secret(cfg.get("client_secret_enc", "")),
    )


# ================== SHAREPOINT PROCESSOR ==================

class SharePointProcessor:
    """Processa le cartelle SharePoint applicando le regole"""

    def __init__(self, config: IngestConfig, sharepoint_job_id, auth: Optional[SharePointAuth] = None):
        self.config = config
        self.sharepoint_job_id = sharepoint_job_id

        # accumulator settings
        self.folder_found = 0
        self.files_found = 0
        self.files_to_process = 0
        self.files_to_skip = 0
        self.total_size = 0

        self.files_failed = 0  # 👈 nuovo contatore

        # Credenziali: dalla source se fornite, altrimenti fallback alle env globali.
        self.client = GraphAPIClient(auth or default_env_auth())

    def should_include_file(self, file_info: Dict[str, Any]) -> Tuple[bool, str]:
        """Determina se un file deve essere incluso basandosi sulle regole"""

        file_name = file_info.get('name', '')

        # Step 0: fail-fast su estensioni non supportate dal parser e file troppo grandi.
        # Evita download inutili e job destinati a FAILED a valle.
        file_ext = os.path.splitext(file_name)[1].lower()
        if file_ext not in PARSER_SUPPORTED_EXTENSIONS:
            return False, f"EXCLUDED - Unsupported extension '{file_ext}'"

        file_size = int(file_info.get("size") or 0)
        if file_size > MAX_FILE_SIZE:
            return False, f"EXCLUDED - File too large ({file_size} bytes > {MAX_FILE_SIZE})"

        # Step 0.5: file marcato EXCLUDED manualmente → la sync lo VEDE ma lo salta
        # (niente download, niente job). Vale anche col cron. Identità durevole =
        # sharepoint_file_id (l'id Graph): il file_id viene rigenerato a ogni sync.
        if is_excluded(self.config.vector_store_id, sharepoint_file_id=file_info.get("id")):
            return False, "EXCLUDED - escluso manualmente"

        # Step 1: INCLUDE rules (whitelist) - se presenti DEVONO matchare
        if self.config.include_rules:
            rules_by_type = {}
            for rule in self.config.include_rules:
                if rule.type not in rules_by_type:
                    rules_by_type[rule.type] = []
                rules_by_type[rule.type].append(rule)

            # Ogni TIPO deve avere almeno un match (AND tra tipi)
            for rule_type, rules in rules_by_type.items():
                type_matched = False

                # All'interno del tipo, basta UN match (OR)
                for rule in rules:
                    if rule.type == "pattern":
                        patterns = [p.strip() for p in rule.value.split(',')]
                        if any(p.lower() in file_name.lower() for p in patterns):
                            type_matched = True
                            break

                    elif rule.type == "regex":
                        if re.match(rule.value, file_name):
                            type_matched = True
                            break

                # Se questo TIPO non ha matchato, escludi il file
                if not type_matched:
                    return False, f"EXCLUDED - No '{rule_type}' rule matched"

        # Step 2: EXCLUDE rules (blacklist) - se matchano TUTTE → escludi
        if self.config.exclude_rules:
            exclude_by_type = {}
            for rule in self.config.exclude_rules:
                if rule.type not in exclude_by_type:
                    exclude_by_type[rule.type] = []
                exclude_by_type[rule.type].append(rule)

            # Ogni tipo deve avere almeno un match (AND tra tipi, OR dentro tipo)
            all_types_matched = True
            for rule_type, rules in exclude_by_type.items():
                type_matched = False

                for rule in rules:
                    if rule.type == "pattern":
                        patterns = [p.strip() for p in rule.value.split(',')]
                        if any(p.lower() in file_name.lower() for p in patterns):
                            type_matched = True
                            break

                    elif rule.type == "regex":
                        if re.match(rule.value, file_name):
                            type_matched = True
                            break

                if not type_matched:
                    all_types_matched = False
                    break

            if all_types_matched:
                return False, f"EXCLUDED - All exclude rule types matched"

        # Step 3: Check database - file già in lavorazione o già processato?
        query = {
            "vector_store_id": self.config.vector_store_id,
            "attributes.sharepoint_file_id": file_info["id"],
            "$or": [
                {"status": {"$in": ["PENDING", "PROCESSING"]}},
                {
                    "status": "COMPLETED",
                    "attributes.sharepoint_file_modified": file_info["modified"],
                },
            ],
        }

        for key, value in (self.config.attributes or {}).items():
            if key in RESERVED_ATTR_KEYS:
                continue
            # la chiave diventa un field name Mongo (attributes.<key>): una con '.'
            # creerebbe un path annidato non voluto, una con '$' un operatore → salta.
            if "." in key or key.startswith("$"):
                logger.warning(f"attributo con chiave non sicura ignorato nel match: {key!r}")
                continue
            query[f"attributes.{key}"] = value

        results = ingestion_jobs.find_one(query)

        if results:
            status = results.get("status")
            attrs = results.get("attributes", {}) or {}
            modified_in_db = attrs.get("sharepoint_file_modified")

            if status in ["PENDING", "PROCESSING"]:
                reason = f"FILE IN {status}"
            elif modified_in_db == file_info["modified"]:
                reason = "FILE UNCHANGED"
                logger.info(
                    f"♻️ UNCHANGED file {file_info['id']} — modified date unchanged. "
                    f"Job: {results.get('_id')}"
                )
            else:
                reason = "FILE BLOCKED"

            return False, f"{reason} - Matched ingestion_jobs: {results.get('_id')}"

        # Step 4: Tutto ok → includi il file!
        return True, "INCLUDED - New file, passed all filters"

    async def superseded_file_ids(self, file_info) -> list:
        """File_id dei vecchi ingest dello stesso file SharePoint da sostituire
        (COMPLETED con modified diversa → reindex, oppure FAILED → retry).

        NON cancella nulla: ritorna solo la lista. La rimozione effettiva
        (punti Qdrant + job + file) la fa il vector worker SOLO a re-ingest
        COMPLETED (`cleanup_superseded`) → niente delete-before-success.
        """
        query = {
            "vector_store_id": self.config.vector_store_id,
            "attributes.sharepoint_file_id": file_info["id"],
            "$or": [
                {
                    "status": "COMPLETED",
                    "attributes.sharepoint_file_modified": {"$ne": file_info["modified"]},
                },
                {"status": "FAILED"},
            ],
        }

        # Restringi alla stessa "directory"/attributi della source
        for key, value in (self.config.attributes or {}).items():
            if key in RESERVED_ATTR_KEYS:
                continue
            # la chiave diventa un field name Mongo (attributes.<key>): una con '.'
            # creerebbe un path annidato non voluto, una con '$' un operatore → salta.
            if "." in key or key.startswith("$"):
                logger.warning(f"attributo con chiave non sicura ignorato nel match: {key!r}")
                continue
            query[f"attributes.{key}"] = value

        old_jobs = await asyncio.to_thread(lambda: list(ingestion_jobs.find(query)))
        return [j["file_id"] for j in old_jobs if j.get("file_id")]

    async def processing_file_job(self, file_info, reason, folder_name, folder_config):
        logger.info(f"✔️ PROCESSING: {file_info['name']} - {reason}")

        # Aggiungi attributi custom
        file_info['attributes'] = self.config.attributes.copy() if self.config.attributes else {}
        file_info['attributes']['source_folder'] = folder_name
        file_info['attributes']['folder_id'] = folder_config.sharepoint_id

        # --- DOWNLOAD FILE DA GRAPH ---
        logger.info(f"  ✓ DOWNLOAD : {file_info['name']} - {reason}")

        drive_id = file_info["_drive_id"]
        item_id = file_info["id"]

        # --- RE-INGEST SICURO ---
        # Se il file ha modified diversa (o un vecchio job FAILED) raccogliamo i
        # file_id da sostituire, SENZA cancellarli ora: il vector worker li
        # rimuove solo a re-ingest COMPLETED. Se il parse fallisce, il vecchio
        # contenuto resta ricercabile.
        supersedes = await self.superseded_file_ids(file_info)

        # Download in streaming verso lo storage finale (no buffer in RAM)
        def _stream_download(output_path: str) -> int:
            return self.client.download_file_to_path(drive_id, item_id, output_path)

        metadata = await store_file_on_disk(
            stream_source=_stream_download,
            filename=file_info["name"],
            content_type=file_info.get("mime_type"),
        )

        file_info["file_id"] = metadata["id"]
        file_info["local_path"] = metadata["path"]

        # prima di chiamare Docling:
        # await wait_for_docling_slot(self.config.vector_store_id)

        # # 2) Upload a Docling (async task)
        # try:
        #     task_info = upload_file_for_chunking_task_async(file_info["local_path"])
        #
        # except Exception as e:
        #     raise HTTPException(status_code=500, detail=f"Failed to create Docling task: {e}")

        # docling_task_id = task_info["task_id"]
        # docling_status = task_info.get("task_status", "queued")

        # 3) Crea job su Mongo
        now_ts = get_timestamp()
        job_doc = {
            "vector_store_id": self.config.vector_store_id,
            "file_id": file_info["file_id"],
            "filename": metadata.get("filename"),
            "file_size": metadata.get("bytes"),
            "content_hash": metadata.get("content_hash"),
            "attributes": {
                **(self.config.attributes or {}),  # 👈 fallback se è None
                "sharepoint_file_id": file_info["id"],
                "sharepoint_file_created": file_info["created"],
                "sharepoint_file_modified": file_info["modified"],
            },

            # Re-ingest sicuro: vecchi file_id sostituiti, rimossi a COMPLETED dal worker
            "supersedes_file_ids": supersedes,

            # SPECIAL PARAMS => TO FIND SHAREPOINTS JOB
            "sharepoint_job_id": self.sharepoint_job_id,

            "file_path": file_info["local_path"],  # new for upload task

            # "parser_task_id": docling_task_id,
            # "parser_status": docling_status,
            # "parser_processing_time": None,
            # "parser_doc_markdown": None,

            "status": "PENDING",
            "error": None,
            "created_at": now_ts,
            "updated_at": now_ts,
            "stats": {
                "num_chunks": 0,
                # "num_pages": 0,
            },
        }

        result = await asyncio.to_thread(ingestion_jobs.insert_one, job_doc)

        logger.info(f"➰ PARSER DOC ID: {result.inserted_id}")

        self.total_size += file_info.get('size', 0)

    async def push_sharepoint_job_file(self, field, file):
        """Aggiunge un file a un campo array del job e aggiorna updated_at"""
        now_ts = get_timestamp()
        update_query = {
            "$addToSet": {field: file},
            "$set": {"updated_at": now_ts},
        }
        await asyncio.to_thread(
            sharepoint_jobs.update_one,
            {"_id": self.sharepoint_job_id},
            update_query,
        )

    async def process_folders(self) -> Dict[str, Any]:
        """Processa tutte le cartelle configurate"""

        # Connetti al sito (Graph API call → off main loop)
        await asyncio.to_thread(self.client.connect_to_site)

        # Processa ogni cartella configurata
        for folder_config in self.config.folders:
            logger.info(f"\n{'=' * 60}")
            logger.info(f"Processing folder: {folder_config.sharepoint_id}")
            logger.info(f"Recursive: {folder_config.recursive}")
            logger.info(f"{'=' * 60}")

            # local accumulator settings
            local_files_to_process = 0
            local_files_to_skip = 0
            local_files_failed = 0

            # Ottieni info sulla cartella (Graph API call → off main loop)
            folder_info = await asyncio.to_thread(
                self.client.get_folder_info, folder_config.sharepoint_id
            )

            if not folder_info:
                logger.error(f"Folder {folder_config.sharepoint_id} not found, skipping...")
                continue

            if 'folder' not in folder_info:
                logger.error(f"Item {folder_config.sharepoint_id} is not a folder, skipping...")
                continue

            folder_name = folder_info.get('name', 'Unknown')
            drive_id = folder_info.get('_drive_id')

            logger.info(f"Processing folder: {folder_name}")

            # Scan completo della cartella in un thread (la paginazione Graph è sync).
            # Per cartelle molto grandi (>100k file) considerare un producer/consumer
            # async; oggi il working set è materializzato in memoria per iterazione.
            files = await asyncio.to_thread(
                lambda: list(self.client.iter_folder_files(
                    drive_id=drive_id,
                    folder_id=folder_config.sharepoint_id,
                    recursive=folder_config.recursive,
                ))
            )

            for file_info in files:
                self.files_found += 1

                # should_include_file fa anche un find_one su mongo → off main loop
                should_include, reason = await asyncio.to_thread(
                    self.should_include_file, file_info
                )

                if should_include:
                    try:
                        await self.processing_file_job(file_info, reason, folder_name, folder_config)
                        self.files_to_process += 1
                        local_files_to_process += 1
                    except Exception as e:
                        self.files_failed += 1
                        local_files_failed += 1
                        logger.exception(
                            f"💥 ERROR PROCESSING: {file_info['name']} - {e}"
                        )
                        await self.push_sharepoint_job_file("skipped_files_lists", {
                            "name": file_info["name"],
                            "reason": f"ERROR: {e}",
                        })
                else:
                    self.files_to_skip += 1
                    local_files_to_skip += 1
                    logger.info(f"🚫 SKIPPING: {file_info['name']} - {reason}")

                    if "UNCHANGED" not in reason:
                        await self.push_sharepoint_job_file("skipped_files_lists", {
                            "name": file_info["name"],
                            "reason": reason,
                        })

            # update global folder data
            self.folder_found += 1
            logger.info(
                f"Folder summary: "
                f"{local_files_to_process}/{local_files_to_process + local_files_to_skip} "
                f"files processed, {local_files_failed} failed"
            )

        # Riassunto finale
        logger.info(f"\n{'=' * 60}")
        logger.info(f"PROCESSING SUMMARY")
        logger.info(f"{'=' * 60}")
        logger.info(f"Total folders processed: {self.folder_found}")
        logger.info(f"Total files discovered: {self.files_found}")
        logger.info(f"Total files processed: {self.files_to_process}")
        logger.info(f"Total files skipped: {self.files_to_skip}")
        logger.info(f"Total files failed: {self.files_failed}")
        logger.info(f"Total size: {self.total_size / (1024 * 1024):.2f} MB")

        if self.config.attributes:
            logger.info(f"\nAttributes to be applied in Qdrant:")
            for key, value in self.config.attributes.items():
                logger.info(f"  - {key}: {value}")

        return {
            'success': True,
            'site_id': self.client.site_id,
            'folders_processed': self.folder_found,
            'total_files': self.files_to_process + self.files_to_skip,
            'processed_files': self.files_to_process,
            'files_failed': self.files_failed,  # 👈 utile in risposta API
            'skipped_files': self.files_to_skip,
            'total_size': self.total_size,
        }
