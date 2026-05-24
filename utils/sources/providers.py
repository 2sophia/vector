"""Provider concreti. Oggi solo SharePoint è `enabled`; gli altri sono
placeholder (config_fields plausibili ma `enabled=False`) per essere
implementati in futuro riempiendo `browse`/download senza toccare il resto.
"""

from typing import Any, Dict, Optional

from .base import ProviderField, SourceProvider

# Document library di sistema di SharePoint (EN + IT): rumore da nascondere
# nel picker. Confronto case-insensitive sul nome.
SHAREPOINT_SYSTEM_LIBRARIES = {
    "site assets", "risorse del sito",
    "site pages", "pagine del sito",
    "style library", "raccolta stili",
    "form templates", "modelli modulo", "modelli di modulo",
    "teams wiki data",
    "preservation hold library", "raccolta blocchi per conservazione",
    "site collection documents", "site collection images",
}


class SharePointProvider(SourceProvider):
    type = "sharepoint"
    label = "SharePoint"
    enabled = True
    config_fields = [
        ProviderField(name="site_url", label="Site URL",
                      placeholder="https://tenant.sharepoint.com/sites/..."),
        ProviderField(name="tenant_id", label="Tenant ID"),
        ProviderField(name="client_id", label="Client ID"),
        ProviderField(name="client_secret", label="Client Secret", type="password",
                      secret=True, placeholder="(cifrato, mai mostrato)"),
    ]

    def browse(self, config: Dict[str, Any], drive_id: Optional[str] = None,
               folder_id: Optional[str] = None) -> Dict[str, Any]:
        # Import locali: evita di caricare la chain SharePoint se non serve.
        from utils.crypto import decrypt_secret
        from utils.sharepoint.ingestion import GraphAPIClient, SharePointAuth

        auth = SharePointAuth(
            site_url=config.get("site_url", ""),
            tenant_id=config.get("tenant_id", ""),
            client_id=config.get("client_id", ""),
            client_secret=decrypt_secret(config.get("client_secret_enc", "")),
        )
        client = GraphAPIClient(auth)
        client.connect_to_site()

        if not drive_id:
            drives = client.get_drives()
            visible = [
                d for d in drives
                if (d.get("name") or "").strip().lower() not in SHAREPOINT_SYSTEM_LIBRARIES
            ]
            return {
                "level": "drives",
                "drives": [{"id": d.get("id"), "name": d.get("name")} for d in visible],
            }
        children = client.list_children(drive_id, folder_id)
        return {
            "level": "folders",
            "drive_id": drive_id,
            "folder_id": folder_id,
            "folders": children.get("folders", []),
            "files": children.get("files", []),
        }


# --- Placeholder: enabled=False, config_fields indicativi, niente browse ---

class GoogleDriveProvider(SourceProvider):
    type = "gdrive"
    label = "Google Drive"
    enabled = False
    config_fields = [
        ProviderField(name="client_id", label="OAuth Client ID"),
        ProviderField(name="client_secret", label="OAuth Client Secret", type="password", secret=True),
        ProviderField(name="refresh_token", label="Refresh Token", type="password", secret=True),
    ]


class GoogleWorkspaceProvider(SourceProvider):
    type = "gworkspace"
    label = "Google Workspace"
    enabled = False
    config_fields = [
        ProviderField(name="service_account_json", label="Service Account JSON", type="password", secret=True),
        ProviderField(name="shared_drive_id", label="Shared Drive ID", required=False),
        ProviderField(name="delegated_user", label="Utente delegato (email)", required=False),
    ]


class S3Provider(SourceProvider):
    type = "s3"
    label = "Amazon S3"
    enabled = False
    config_fields = [
        ProviderField(name="bucket", label="Bucket"),
        ProviderField(name="region", label="Region", placeholder="eu-south-1"),
        ProviderField(name="access_key_id", label="Access Key ID"),
        ProviderField(name="secret_access_key", label="Secret Access Key", type="password", secret=True),
    ]
