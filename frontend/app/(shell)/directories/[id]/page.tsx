"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  Ban,
  Check,
  CheckCircle2,
  CheckSquare,
  CloudUpload,
  ExternalLink,
  FileText,
  FileWarning,
  FolderTree,
  Loader2,
  Pencil,
  Plug,
  RefreshCw,
  RotateCcw,
  Search,
  Sparkles,
  Trash2,
  Undo2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { SourceBrowser, type BrowseFolder } from "@/components/source-browser";
import { SchemaEditor } from "@/components/schema-editor";
import { Dialog, DialogBody, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const SLUG_FIELD = "sophia_directory_slug";

type Source = { id: string; name: string; type: string };
type SpJob = {
  id: string;
  status: string;
  vector_store_id: string;
  source_id?: string;
  attributes?: Record<string, unknown>;
  folders?: { sharepoint_id: string; name?: string; recursive?: boolean }[];
  skipped_files_lists?: { name: string; reason: string }[];
  total_files: number;
  processed_files: number;
  files_failed: number;
};
type FolderRow = { sharepoint_id: string; name: string; recursive: boolean };

type Directory = {
  id: string;
  name: string;
  slug: string;
  properties: Record<string, unknown>;
  vector_store_id: string;
  file_count: number;
};
type StoreFile = {
  id: string;
  file_id: string;
  filename: string;
  status: string;
  num_chunks: number;
  /** motivo del fallimento (se status === "FAILED") */
  error?: string | null;
  created_at?: number;
  usage_bytes?: number;
  attributes?: Record<string, unknown>;
  /** valorizzato se il file proviene da una sync SharePoint */
  sharepoint_job_id?: string | null;
};

/** Un file è "sincronizzato" se è legato a una sync (non caricato a mano). */
function isSynced(f: StoreFile): boolean {
  return Boolean(f.sharepoint_job_id || f.attributes?.sharepoint_file_id);
}

const STATUS_PILL: Record<string, string> = {
  PENDING: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  PROCESSING: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  COMPLETED: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  FAILED: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  EXCLUDED: "bg-zinc-200 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400",
};

// Ordinamento: in cima ciò che è "vivo" o da gestire, in fondo ciò che è a posto
// o messo da parte di proposito (escluso).
const STATUS_RANK: Record<string, number> = {
  PROCESSING: 0,
  FAILED: 1,
  PENDING: 2,
  COMPLETED: 3,
  EXCLUDED: 4,
};
type SortMode = "status" | "name" | "recent";
const SORT_LABEL: Record<SortMode, string> = { status: "Stato", name: "Nome", recent: "Recenti" };

// Filtro per stato (chip nella card file). "ALL" = nessun filtro.
const STATUS_FILTERS: { key: string; label: string }[] = [
  { key: "ALL", label: "Tutti" },
  { key: "COMPLETED", label: "Completi" },
  { key: "PROCESSING", label: "In corso" },
  { key: "PENDING", label: "In coda" },
  { key: "FAILED", label: "Falliti" },
  { key: "EXCLUDED", label: "Esclusi" },
];

function sortFiles(rows: StoreFile[], mode: SortMode): StoreFile[] {
  const arr = [...rows];
  if (mode === "name") {
    arr.sort((a, b) => a.filename.localeCompare(b.filename, "it"));
  } else if (mode === "recent") {
    arr.sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));
  } else {
    arr.sort(
      (a, b) =>
        (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9) ||
        a.filename.localeCompare(b.filename, "it"),
    );
  }
  return arr;
}

function fmtBytes(n?: number): string {
  if (!n || n <= 0) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

function fmtDate(ts?: number): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleDateString("it-IT", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

/** Barra di avanzamento multi-segmento (completed/processing/pending/failed)
 *  derivata dagli status reali degli ingestion_job. Riusata per la directory
 *  intera e per i file di una singola sync. */
function StatusBar({ rows }: { rows: StoreFile[] }) {
  const total = rows.length;
  if (!total) return null;
  const count = (s: string) => rows.filter((f) => f.status === s).length;
  const completed = count("COMPLETED");
  const processing = count("PROCESSING");
  const failed = count("FAILED");
  const excluded = count("EXCLUDED");
  const pending = total - completed - processing - failed - excluded; // PENDING + eventuali unknown
  const active = processing > 0 || pending > 0;
  const pct = (n: number) => `${(n / total) * 100}%`;

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span className="inline-flex items-center gap-1.5 font-medium text-zinc-700 dark:text-zinc-300">
          {active && <Loader2 className="size-3 animate-spin text-amber-500" />}
          {completed}/{total} indicizzati
        </span>
        <span className="flex flex-wrap items-center justify-end gap-x-2 text-zinc-500 dark:text-zinc-400">
          {processing > 0 && (
            <span className="text-amber-600 dark:text-amber-400">{processing} in corso</span>
          )}
          {pending > 0 && <span>{pending} in coda</span>}
          {failed > 0 && <span className="text-red-600 dark:text-red-400">{failed} falliti</span>}
          {excluded > 0 && <span className="text-zinc-400 dark:text-zinc-500">{excluded} esclusi</span>}
        </span>
      </div>
      <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800">
        {completed > 0 && <div className="bg-emerald-500" style={{ width: pct(completed) }} />}
        {processing > 0 && <div className="bg-amber-400" style={{ width: pct(processing) }} />}
        {pending > 0 && <div className="bg-zinc-300 dark:bg-zinc-600" style={{ width: pct(pending) }} />}
        {failed > 0 && <div className="bg-red-500" style={{ width: pct(failed) }} />}
        {excluded > 0 && <div className="bg-zinc-400 dark:bg-zinc-500" style={{ width: pct(excluded) }} />}
      </div>
    </div>
  );
}

const FILES_PAGE_SIZE = 100;

/** Checkbox "seleziona tutto" di un gruppo, con stato indeterminate quando solo
 *  alcune righe sono selezionate (gestito via ref, non esiste come prop su <input>). */
function SelectAllBox({
  rows,
  selected,
  onToggleAll,
}: {
  rows: StoreFile[];
  selected: Set<string>;
  onToggleAll: (rows: StoreFile[], checked: boolean) => void;
}) {
  const all = rows.length > 0 && rows.every((f) => selected.has(f.file_id));
  const some = rows.some((f) => selected.has(f.file_id));
  return (
    <input
      type="checkbox"
      className="size-4 cursor-pointer accent-indigo-600"
      checked={all}
      ref={(el) => {
        if (el) el.indeterminate = !all && some;
      }}
      onChange={(e) => onToggleAll(rows, e.target.checked)}
      aria-label="Seleziona tutti"
    />
  );
}

/** Tabella file riusabile: stessa resa per i file manuali e per i sincronizzati.
 *  Pagina lato client (FILES_PAGE_SIZE per volta) per non montare migliaia di righe. */
function FilesTable({
  rows,
  selected,
  onToggle,
  onToggleAll,
  onDelete,
  onRetry,
  onExclude,
  onUnexclude,
}: {
  rows: StoreFile[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onToggleAll: (rows: StoreFile[], checked: boolean) => void;
  onDelete: (f: StoreFile) => void;
  onRetry: (f: StoreFile) => void;
  onExclude: (f: StoreFile) => void;
  onUnexclude: (f: StoreFile) => void;
}) {
  const [visible, setVisible] = useState(FILES_PAGE_SIZE);
  const shown = rows.slice(0, visible);
  const remaining = rows.length - visible;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b border-zinc-200 text-left text-xs uppercase text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
          <tr>
            <th className="w-8 px-2 py-2">
              <SelectAllBox rows={rows} selected={selected} onToggleAll={onToggleAll} />
            </th>
            <th className="px-2 py-2 font-medium">File</th>
            <th className="px-2 py-2 text-right font-medium">Chunks</th>
            <th className="px-2 py-2 font-medium">Stato</th>
            <th className="px-2 py-2"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {shown.map((f) => (
            <tr
              key={f.id}
              className={cn(
                "group transition-colors hover:bg-zinc-50 dark:hover:bg-zinc-900/40",
                selected.has(f.file_id) && "bg-indigo-50/60 dark:bg-indigo-950/20",
              )}
            >
              <td className="px-2 py-2.5 align-top">
                <input
                  type="checkbox"
                  className="mt-0.5 size-4 cursor-pointer accent-indigo-600"
                  checked={selected.has(f.file_id)}
                  onChange={() => onToggle(f.file_id)}
                  aria-label={`Seleziona ${f.filename}`}
                />
              </td>
              <td className="max-w-md px-2 py-2.5">
                <div className="flex items-start gap-2">
                  <FileText className="mt-0.5 size-4 shrink-0 text-zinc-400" />
                  <div className="min-w-0">
                    <a
                      href={`/api/backend/files/${f.file_id}/content?inline=1`}
                      target="_blank"
                      rel="noopener noreferrer"
                      title="Apri il file"
                      className="inline-flex max-w-full items-center gap-1 truncate font-medium hover:text-indigo-600 hover:underline dark:hover:text-indigo-400"
                    >
                      <span className="truncate">{f.filename}</span>
                      <ExternalLink className="size-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-60" />
                    </a>
                    <div className="flex flex-wrap items-center gap-x-2 text-[11px] text-zinc-500 dark:text-zinc-400">
                      <span className="font-mono">{f.file_id}</span>
                      {f.usage_bytes ? <span>· {fmtBytes(f.usage_bytes)}</span> : null}
                      {f.created_at ? <span>· {fmtDate(f.created_at)}</span> : null}
                    </div>
                  </div>
                </div>
              </td>
              <td className="px-2 py-2.5 text-right text-zinc-600 dark:text-zinc-400">{f.num_chunks}</td>
              <td className="px-2 py-2.5">
                <span
                  className={cn(
                    "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
                    STATUS_PILL[f.status] || STATUS_PILL.PENDING,
                  )}
                  title={f.status === "FAILED" ? f.error ?? undefined : undefined}
                >
                  {f.status === "COMPLETED" && <CheckCircle2 className="size-3" />}
                  {f.status === "PROCESSING" && <Loader2 className="size-3 animate-spin" />}
                  {f.status === "FAILED" && <FileWarning className="size-3" />}
                  {f.status === "EXCLUDED" && <Ban className="size-3" />}
                  {f.status}
                </span>
                {f.status === "FAILED" && f.error && (
                  <div className="mt-1 max-w-[22rem] truncate text-[11px] text-red-600 dark:text-red-400" title={f.error}>
                    {f.error}
                  </div>
                )}
              </td>
              <td className="px-2 py-2.5 text-right">
                {f.status === "FAILED" && (
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => onRetry(f)}
                    aria-label="Riprova"
                    title="Ri-accoda il file (torna in coda per un nuovo tentativo)"
                  >
                    <RotateCcw className="size-4 text-zinc-500 hover:text-indigo-600" />
                  </Button>
                )}
                {f.status === "EXCLUDED" ? (
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => onUnexclude(f)}
                    aria-label="Re-includi"
                    title="Togli l'esclusione (poi re-attacca o re-sincronizza per re-ingestire)"
                  >
                    <Undo2 className="size-4 text-zinc-500 hover:text-emerald-600" />
                  </Button>
                ) : (
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => onExclude(f)}
                    aria-label="Escludi"
                    title="Escludi: worker e sync lo salteranno (anche col cron) e i dati indicizzati vengono rimossi"
                  >
                    <Ban className="size-4 text-zinc-500 hover:text-amber-600" />
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => onDelete(f)}
                  aria-label="Rimuovi"
                  title={
                    isSynced(f)
                      ? "Tornerà al prossimo sync se ancora presente nella source"
                      : "Rimuovi dalla directory"
                  }
                >
                  <Trash2 className="size-4 text-zinc-500 hover:text-red-600" />
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {remaining > 0 && (
        <div className="flex justify-center pt-3">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setVisible((v) => v + FILES_PAGE_SIZE)}
          >
            Mostra altri ({Math.min(remaining, FILES_PAGE_SIZE)} di {remaining})
          </Button>
        </div>
      )}
    </div>
  );
}

export default function DirectoryDetailPage() {
  const params = useParams<{ id: string }>();
  const directoryId = params.id;
  // Modalità speciale "senza directory": pseudo-directory per i file dello store NON
  // assegnati a nessuno slug (caricati via API/dev). Lo store arriva dal query param ?vs=.
  const searchParams = useSearchParams();
  const isUnassigned = directoryId === "_unassigned";
  const vsParam = searchParams.get("vs") || "";
  const [dirSlugs, setDirSlugs] = useState<string[]>([]);

  const [dir, setDir] = useState<Directory | null>(null);
  const [storeName, setStoreName] = useState<string>("");
  const [files, setFiles] = useState<StoreFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showSchema, setShowSchema] = useState(false);
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  async function saveName() {
    if (!dir) return;
    const n = nameDraft.trim();
    if (!n || n === dir.name) {
      setEditingName(false);
      return;
    }
    try {
      await api.patch(`/directories/${dir.id}`, { name: n });
      setDir({ ...dir, name: n });
      setEditingName(false);
    } catch (e) {
      setError(String(e));
    }
  }

  // Import da sorgente
  const [sources, setSources] = useState<Source[]>([]);
  const [sourceId, setSourceId] = useState<string>("");
  const [folders, setFolders] = useState<FolderRow[]>([]);
  const [spJobs, setSpJobs] = useState<SpJob[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [resyncingId, setResyncingId] = useState<string | null>(null);
  const [schemaSyncId, setSchemaSyncId] = useState<string | null>(null);
  const [browseOpen, setBrowseOpen] = useState(false);
  const [sortMode, setSortMode] = useState<SortMode>("status");
  const [fileQuery, setFileQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("ALL");
  // `accept` del file picker: viene dalla source of truth backend
  // (/files/supported-formats). Fallback prudente se l'endpoint non risponde.
  const [acceptAttr, setAcceptAttr] = useState<string>(
    ".pdf,.docx,.pptx,.html,.htm,.xlsx,.md,.png,.jpg,.jpeg,.tiff,.bmp,.gif",
  );

  // Sources disponibili
  useEffect(() => {
    (async () => {
      try {
        const res = await api.get<{ data: Source[] }>("/sources");
        const list = res.data || [];
        setSources(list);
        setSourceId((cur) => cur || (list[0]?.id ?? ""));
      } catch {
        /* sources opzionali */
      }
    })();
  }, []);

  // Estensioni accettate dalla source of truth backend (niente lista duplicata).
  useEffect(() => {
    (async () => {
      try {
        const f = await api.get<{ extensions: string[] }>("/files/supported-formats");
        if (f.extensions?.length) setAcceptAttr(f.extensions.join(","));
      } catch {
        /* resta il fallback */
      }
    })();
  }, []);

  // Job di import di QUESTA directory (stesso vector store + slug)
  const refreshSpJobs = useCallback(async () => {
    if (!dir || isUnassigned) return;
    try {
      const res = await api.get<{ data: SpJob[] }>("/ingest/sharepoint");
      const mine = (res.data || []).filter(
        (j) =>
          j.vector_store_id === dir.vector_store_id &&
          (j.attributes?.[SLUG_FIELD] ?? "") === dir.slug,
      );
      setSpJobs(mine);
    } catch {
      /* ignore */
    }
  }, [dir, isUnassigned]);

  // Carica la directory (o costruisce la pseudo-directory "senza slug")
  useEffect(() => {
    if (isUnassigned) {
      // Nessun doc su Mongo: pseudo-directory sintetica. Servono gli slug reali dello
      // store per filtrare i file che NON appartengono a nessuna directory.
      setDir({
        id: "_unassigned",
        name: "Senza directory",
        slug: "",
        properties: {},
        vector_store_id: vsParam,
        file_count: 0,
      } as Directory);
      (async () => {
        try {
          const res = await api.get<{ data: Directory[] }>(
            `/directories?vector_store_id=${vsParam}`,
          );
          setDirSlugs((res.data || []).map((d) => d.slug));
        } catch {
          setDirSlugs([]);
        }
      })();
      return;
    }
    (async () => {
      try {
        const d = await api.get<Directory>(`/directories/${directoryId}`);
        setDir(d);
      } catch (e) {
        setError(String(e));
        setLoading(false);
      }
    })();
  }, [directoryId, isUnassigned, vsParam]);

  // Nome del vector store per il breadcrumb
  useEffect(() => {
    if (!dir) return;
    (async () => {
      try {
        const s = await api.get<{ name: string }>(`/vector_stores/${dir.vector_store_id}`);
        setStoreName(s.name || "");
      } catch {
        /* breadcrumb resta generico */
      }
    })();
  }, [dir]);

  // File dello store: in una directory normale quelli col suo slug; nella pseudo
  // "senza directory" quelli che NON appartengono a nessuno slug esistente.
  const refresh = useCallback(async () => {
    if (!dir) return;
    setLoading(true);
    try {
      const res = await api.get<{ data: StoreFile[] }>(`/vector_stores/${dir.vector_store_id}/files`);
      const all = res.data || [];
      const mine = isUnassigned
        ? all.filter((f) => !dirSlugs.includes(String(f.attributes?.[SLUG_FIELD] ?? "")))
        : all.filter((f) => (f.attributes?.[SLUG_FIELD] ?? "") === dir.slug);
      setFiles(mine);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [dir, isUnassigned, dirSlugs]);

  // Fetch iniziale una volta quando la directory è pronta.
  useEffect(() => {
    if (!dir) return;
    refresh();
    refreshSpJobs();
  }, [dir, refresh, refreshSpJobs]);

  // C'è lavoro in corso? Solo allora ha senso fare polling.
  const isActive = (s?: string) => s === "PENDING" || s === "PROCESSING";
  const hasActivity =
    files.some((f) => isActive(f.status)) || spJobs.some((j) => isActive(j.status));

  // Polling adattivo: gira a 5s solo finché c'è roba in lavorazione, poi si ferma
  // (riparte da solo quando avvii una sync o carichi file → tornano stati attivi).
  useEffect(() => {
    if (!dir || !hasActivity) return;
    const id = setInterval(() => {
      refresh();
      refreshSpJobs();
    }, 5000);
    return () => clearInterval(id);
  }, [dir, hasActivity, refresh, refreshSpJobs]);

  async function handleFiles(fileList: FileList | File[]) {
    if (!dir) return;
    setError(null);
    setNotice(null);
    setUploading(true);
    const attributes: Record<string, unknown> = { [SLUG_FIELD]: dir.slug, ...dir.properties };
    let skipped = 0;
    try {
      for (const file of Array.from(fileList)) {
        const form = new FormData();
        form.append("file", file);
        const up = await fetch("/api/backend/files", { method: "POST", body: form });
        if (!up.ok) {
          const t = await up.text().catch(() => "");
          setError(`${file.name}: upload ${up.status} ${t || up.statusText}`);
          continue;
        }
        const uploaded = await up.json();
        const res = await api.post<{ deduplicated?: boolean }>(
          `/vector_stores/${dir.vector_store_id}/files`,
          { file_id: uploaded.id, attributes },
        );
        if (res?.deduplicated) skipped++;
      }
      await refresh();
      if (skipped > 0) setNotice(`${skipped} file già presente/i (stesso contenuto): saltato/i.`);
    } catch (e) {
      setError(String(e));
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function handleDelete(f: StoreFile) {
    if (!dir) return;
    if (!confirm(`Rimuovere "${f.filename}" dalla directory?`)) return;
    try {
      await api.delete(`/vector_stores/${dir.vector_store_id}/files/${f.file_id}`);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  async function handleRetry(f: StoreFile) {
    if (!dir) return;
    try {
      await api.post(`/vector_stores/${dir.vector_store_id}/files/${f.file_id}/retry`);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  async function handleExclude(f: StoreFile) {
    if (!dir) return;
    if (
      !confirm(
        `Escludere "${f.filename}"?\n\nIl sistema lo salterà ovunque (vector worker + sync SharePoint, anche col cron) e rimuoverà i dati già indicizzati. Reversibile dal pulsante "Re-includi".`,
      )
    )
      return;
    try {
      await api.post(`/vector_stores/${dir.vector_store_id}/files/${f.file_id}/exclude`);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  async function handleUnexclude(f: StoreFile) {
    if (!dir) return;
    try {
      await api.delete(`/vector_stores/${dir.vector_store_id}/files/${f.file_id}/exclude`);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  // ---- Selezione massiva (azioni su più file insieme) ----
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState<{ action: string; done: number; total: number } | null>(null);

  const toggle = (id: string) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  const toggleAll = (rows: StoreFile[], checked: boolean) =>
    setSelected((s) => {
      const n = new Set(s);
      rows.forEach((f) => (checked ? n.add(f.file_id) : n.delete(f.file_id)));
      return n;
    });
  const clearSel = () => setSelected(new Set());

  // Dopo un refresh i file possono sparire (delete/sync): togli dalla selezione i fantasmi.
  useEffect(() => {
    setSelected((s) => {
      if (s.size === 0) return s;
      const ids = new Set(files.map((f) => f.file_id));
      const n = new Set<string>();
      s.forEach((id) => ids.has(id) && n.add(id));
      return n.size === s.size ? s : n;
    });
  }, [files]);

  // Esegue un'azione su molti file con concorrenza limitata + progress, poi refresh.
  async function runBulk(
    action: string,
    targets: StoreFile[],
    fn: (f: StoreFile) => Promise<unknown>,
  ) {
    if (!dir || targets.length === 0) return;
    setError(null);
    setNotice(null);
    setBulkBusy({ action, done: 0, total: targets.length });
    let done = 0;
    let failed = 0;
    const CONC = 4;
    for (let i = 0; i < targets.length; i += CONC) {
      const batch = targets.slice(i, i + CONC);
      const res = await Promise.allSettled(batch.map(fn));
      res.forEach((r) => {
        done++;
        if (r.status === "rejected") failed++;
      });
      setBulkBusy({ action, done, total: targets.length });
    }
    setBulkBusy(null);
    clearSel();
    await refresh();
    setNotice(`${action}: ${done - failed}/${targets.length} ok${failed ? `, ${failed} non riusciti` : ""}.`);
  }

  function bulkRetry() {
    const t = files.filter((f) => selected.has(f.file_id) && f.status === "FAILED");
    return runBulk("Riprova", t, (f) =>
      api.post(`/vector_stores/${dir!.vector_store_id}/files/${f.file_id}/retry`));
  }
  function bulkExclude() {
    const t = files.filter((f) => selected.has(f.file_id) && f.status !== "EXCLUDED");
    if (
      !confirm(
        `Escludere ${t.length} file?\n\nVerranno saltati ovunque (vector worker + sync, anche col cron) e i dati già indicizzati rimossi. Reversibile con "Re-includi".`,
      )
    )
      return;
    return runBulk("Escludi", t, (f) =>
      api.post(`/vector_stores/${dir!.vector_store_id}/files/${f.file_id}/exclude`));
  }
  function bulkUnexclude() {
    const t = files.filter((f) => selected.has(f.file_id) && f.status === "EXCLUDED");
    return runBulk("Re-includi", t, (f) =>
      api.delete(`/vector_stores/${dir!.vector_store_id}/files/${f.file_id}/exclude`));
  }
  function bulkDelete() {
    const t = files.filter((f) => selected.has(f.file_id));
    if (!confirm(`Rimuovere ${t.length} file dalla directory?`)) return;
    return runBulk("Elimina", t, (f) =>
      api.delete(`/vector_stores/${dir!.vector_store_id}/files/${f.file_id}`));
  }

  async function handleRetryAll() {
    if (!dir) return;
    if (!confirm("Ri-accodare tutti i file falliti di questa directory?")) return;
    try {
      const r = await api.post<{ requeued: number }>(
        `/vector_stores/${dir.vector_store_id}/retry-failed`,
        { slug: dir.slug },
      );
      setNotice(`${r?.requeued ?? 0} file ri-accodati.`);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  function addFolder(f: BrowseFolder) {
    setFolders((arr) =>
      arr.some((x) => x.sharepoint_id === f.id)
        ? arr
        : [...arr, { sharepoint_id: f.id, name: f.name, recursive: true }],
    );
  }

  async function handleStartSync() {
    if (!dir || !sourceId || folders.length === 0) {
      setError("Seleziona una source e almeno una cartella.");
      return;
    }
    setSyncing(true);
    setError(null);
    const attributes: Record<string, unknown> = { [SLUG_FIELD]: dir.slug, ...dir.properties };
    try {
      await api.post("/ingest/sharepoint", {
        vector_store_id: dir.vector_store_id,
        source_id: sourceId,
        folders: folders.map((f) => ({
          sharepoint_id: f.sharepoint_id,
          name: f.name,
          recursive: f.recursive,
        })),
        attributes,
      });
      setFolders([]);
      setBrowseOpen(false);
      setNotice("Import avviato: i file compaiono qui sotto man mano che vengono indicizzati.");
      await refreshSpJobs();
    } catch (e) {
      setError(String(e));
    } finally {
      setSyncing(false);
    }
  }

  async function handleResync(job: SpJob) {
    if (resyncingId) return; // evita doppi click mentre parte
    setResyncingId(job.id);
    try {
      await api.post(`/ingest/sharepoint/${job.id}/sync`);
      await refreshSpJobs();
    } catch (e) {
      setError(String(e));
    } finally {
      setResyncingId(null);
    }
  }

  async function handleDeleteSync(job: SpJob) {
    if (!confirm("Eliminare questa sync? Le cartelle non verranno più sincronizzate.")) return;
    const purge = confirm(
      "Rimuovere anche i file già importati da questa sync?\n\nOK = rimuovi file e chunk · Annulla = tienili nella directory",
    );
    try {
      await api.delete(`/ingest/sharepoint/${job.id}?purge=${purge}`);
      await refreshSpJobs();
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  const propEntries = Object.entries(dir?.properties || {});
  const sourceLabel = (id?: string) => sources.find((s) => s.id === id)?.name || id || "—";

  // Conteggi per stato (su tutti i file, per i badge del filtro).
  const statusCounts = files.reduce<Record<string, number>>((acc, f) => {
    acc[f.status] = (acc[f.status] || 0) + 1;
    return acc;
  }, {});

  // Applica ricerca per nome + filtro stato, poi separa manuali/sync e ordina.
  const q = fileQuery.trim().toLowerCase();
  const visibleFiles = files.filter(
    (f) =>
      (statusFilter === "ALL" || f.status === statusFilter) &&
      (!q || f.filename.toLowerCase().includes(q)),
  );
  const manualFiles = sortFiles(visibleFiles.filter((f) => !isSynced(f)), sortMode);
  const syncedFiles = sortFiles(visibleFiles.filter(isSynced), sortMode);
  const filtering = statusFilter !== "ALL" || q.length > 0;
  const totalManual = files.reduce((n, f) => n + (isSynced(f) ? 0 : 1), 0);
  const totalSynced = files.length - totalManual;
  const shownCount = manualFiles.length + syncedFiles.length;
  // Quanti file indicizzati appartengono a una specifica sync.
  const syncedCount = (jobId: string) =>
    files.filter((f) => f.sharepoint_job_id === jobId).length;

  return (
    <div className="px-8 py-10">
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <Link
          href={dir ? `/stores/${dir.vector_store_id}` : "/stores"}
          className="inline-flex items-center gap-1.5 text-sm text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
        >
          <ArrowLeft className="size-4" />
          {storeName || "Vector store"}
        </Link>

        <div className="flex items-end justify-between gap-4">
          <div className="flex flex-col gap-1.5">
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <FolderTree className="size-6 shrink-0 text-indigo-500" />
              {editingName && dir ? (
                <span className="flex items-center gap-1.5">
                  <Input
                    value={nameDraft}
                    onChange={(e) => setNameDraft(e.target.value)}
                    autoFocus
                    onKeyDown={(e) => {
                      if (e.key === "Enter") saveName();
                      else if (e.key === "Escape") setEditingName(false);
                    }}
                    className="h-9 w-64 text-xl"
                  />
                  <Button size="icon" variant="ghost" onClick={saveName} aria-label="Salva nome">
                    <Check className="size-4 text-emerald-600" />
                  </Button>
                  <Button size="icon" variant="ghost" onClick={() => setEditingName(false)} aria-label="Annulla">
                    <X className="size-4 text-zinc-500" />
                  </Button>
                </span>
              ) : (
                <>
                  {dir?.name ?? "…"}
                  {dir && !isUnassigned && (
                    <button
                      onClick={() => {
                        setNameDraft(dir.name);
                        setEditingName(true);
                      }}
                      className="text-zinc-300 transition-colors hover:text-indigo-600 dark:text-zinc-600"
                      aria-label="Rinomina directory"
                      title="Rinomina"
                    >
                      <Pencil className="size-4" />
                    </button>
                  )}
                </>
              )}
            </h1>
            <div className="flex flex-wrap items-center gap-1.5">
              {dir &&
                (isUnassigned ? (
                  <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-[11px] text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                    file senza slug
                  </span>
                ) : (
                  <span className="rounded-full bg-indigo-50 px-2 py-0.5 font-mono text-[11px] text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300">
                    {dir.slug}
                  </span>
                ))}
              {propEntries.map(([k, v]) => (
                <span
                  key={k}
                  className="rounded-full bg-zinc-100 px-2 py-0.5 text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"
                >
                  <span className="font-mono">{k}</span>={String(v)}
                </span>
              ))}
            </div>
          </div>
          <div className="flex gap-2">
            {!isUnassigned && (
              <Button
                variant={showSchema ? "default" : "outline"}
                size="sm"
                onClick={() => setShowSchema((v) => !v)}
              >
                <Sparkles className="size-4" />
                Estrazione
              </Button>
            )}
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading || !dir}>
              <RefreshCw className={cn("size-4", loading && "animate-spin")} />
              Refresh
            </Button>
          </div>
        </div>

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
            {error}
          </div>
        )}
        {notice && (
          <div className="rounded-md border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs text-indigo-700 dark:border-indigo-900/50 dark:bg-indigo-950/30 dark:text-indigo-300">
            {notice}
          </div>
        )}

        {/* Estrazione (schema entità/relazioni a livello directory) — apribile dalla toolbar */}
        {showSchema && !isUnassigned && (
          <SchemaEditor basePath={`/directories/${directoryId}`} levelLabel="directory" canReset />
        )}

        {/* Upload manuale — solo con uno slug (i file ereditano slug/proprietà della directory) */}
        {!isUnassigned && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Carica documenti</CardTitle>
            <CardDescription>
              I file ereditano le proprietà della directory. PDF, DOCX, PPTX, HTML, XLSX, MD, immagini. Max 512 MB.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <label
              htmlFor="file-upload"
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                if (e.dataTransfer.files?.length) handleFiles(e.dataTransfer.files);
              }}
              className={cn(
                "flex cursor-pointer flex-col items-center justify-center gap-3 rounded-lg border-2 border-dashed px-6 py-10 transition-colors",
                !dir && "pointer-events-none opacity-50",
                dragOver
                  ? "border-indigo-400 bg-indigo-50 dark:border-indigo-500 dark:bg-indigo-950/30"
                  : "border-zinc-300 hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-900",
              )}
            >
              {uploading ? (
                <Loader2 className="size-8 animate-spin text-indigo-500" />
              ) : (
                <CloudUpload className="size-8 text-zinc-400" />
              )}
              <div className="flex flex-col items-center gap-1 text-center">
                <span className="text-sm font-medium">
                  {uploading ? "Upload in corso…" : "Trascina i file qui"}
                </span>
                <span className="text-xs text-zinc-500 dark:text-zinc-400">oppure clicca per selezionare</span>
              </div>
              <input
                ref={inputRef}
                id="file-upload"
                type="file"
                multiple
                accept={acceptAttr}
                className="sr-only"
                disabled={!dir}
                onChange={(e) => e.target.files?.length && handleFiles(e.target.files)}
              />
            </label>
          </CardContent>
        </Card>
        )}

        {/* Import da sorgente */}
        {!isUnassigned && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Plug className="size-4 text-zinc-400" />
              Importa da una sorgente
            </CardTitle>
            <CardDescription>
              Sincronizza i file da una source esterna in questa directory: ereditano slug e proprietà.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            {sources.length === 0 ? (
              <p className="text-sm text-zinc-500 dark:text-zinc-400">
                Nessuna source configurata. Aggiungine una in{" "}
                <Link href="/sources" className="text-indigo-600 hover:underline">
                  Sources
                </Link>
                .
              </p>
            ) : (
              <>
                <div className="flex flex-wrap items-end gap-2">
                  <div className="flex min-w-[16rem] flex-1 flex-col gap-1.5">
                    <Label htmlFor="src-select">Source</Label>
                    <Select value={sourceId} onValueChange={setSourceId}>
                      <SelectTrigger id="src-select">
                        <SelectValue placeholder="Scegli una source" />
                      </SelectTrigger>
                      <SelectContent>
                        {sources.map((s) => (
                          <SelectItem key={s.id} value={s.id}>
                            {s.name} · {s.type}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  {!browseOpen && (
                    <Button
                      variant="outline"
                      onClick={() => (sourceId ? setBrowseOpen(true) : setError("Seleziona una source."))}
                    >
                      <FolderTree className="size-4" />
                      Sfoglia cartelle
                    </Button>
                  )}
                </div>

                {folders.length > 0 && (
                  <div className="flex flex-col gap-1.5">
                    <Label>Cartelle selezionate</Label>
                    {folders.map((f) => (
                      <div
                        key={f.sharepoint_id}
                        className="flex items-center justify-between gap-2 rounded-md border border-zinc-200 px-2 py-1.5 text-sm dark:border-zinc-800"
                      >
                        <span className="inline-flex items-center gap-2 truncate">
                          <FolderTree className="size-4 text-indigo-500" />
                          {f.name}
                        </span>
                        <div className="flex items-center gap-3">
                          <label className="inline-flex items-center gap-1 text-[11px] text-zinc-500 dark:text-zinc-400">
                            <input
                              type="checkbox"
                              checked={f.recursive}
                              onChange={(e) =>
                                setFolders((arr) =>
                                  arr.map((x) =>
                                    x.sharepoint_id === f.sharepoint_id
                                      ? { ...x, recursive: e.target.checked }
                                      : x,
                                  ),
                                )
                              }
                            />
                            sottocartelle
                          </label>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() =>
                              setFolders((arr) => arr.filter((x) => x.sharepoint_id !== f.sharepoint_id))
                            }
                            aria-label="Rimuovi"
                          >
                            <X className="size-4 text-zinc-500 hover:text-red-600" />
                          </Button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {browseOpen && (
                  <SourceBrowser
                    sourceId={sourceId}
                    selectedIds={folders.map((f) => f.sharepoint_id)}
                    onAdd={addFolder}
                    onClose={() => setBrowseOpen(false)}
                  />
                )}

                <div>
                  <Button size="sm" onClick={handleStartSync} disabled={syncing || folders.length === 0}>
                    {syncing && <Loader2 className="size-4 animate-spin" />}
                    Avvia import ({folders.length})
                  </Button>
                </div>

                {spJobs.length > 0 && (
                  <div className="flex flex-col gap-2">
                    <Label>Import di questa directory</Label>
                    <div className="flex flex-col gap-2">
                      {spJobs.map((j) => {
                        const skipped = j.skipped_files_lists || [];
                        const jobFiles = files.filter((f) => f.sharepoint_job_id === j.id);
                        return (
                          <div
                            key={j.id}
                            className="rounded-md border border-zinc-200 p-3 text-sm dark:border-zinc-800"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="inline-flex items-center gap-2">
                                <span
                                  className={cn(
                                    "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
                                    STATUS_PILL[j.status] || STATUS_PILL.PENDING,
                                  )}
                                >
                                  {j.status}
                                </span>
                                <span className="text-xs text-zinc-500 dark:text-zinc-400">
                                  {j.processed_files}/{j.total_files} accodati
                                  {" · "}
                                  <span className="font-medium text-zinc-700 dark:text-zinc-300">
                                    {syncedCount(j.id)} in questa directory
                                  </span>
                                  {j.files_failed ? ` · ${j.files_failed} falliti` : ""}
                                </span>
                              </span>
                              <div className="flex items-center gap-1">
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => setSchemaSyncId(j.id)}
                                  title="Schema di estrazione per questa sync"
                                >
                                  <Sparkles className="size-4" />
                                  Estrazione
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => handleResync(j)}
                                  disabled={resyncingId === j.id || j.status === "PROCESSING"}
                                >
                                  {resyncingId === j.id ? (
                                    <Loader2 className="size-4 animate-spin" />
                                  ) : (
                                    <RefreshCw className="size-4" />
                                  )}
                                  Sync
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  onClick={() => handleDeleteSync(j)}
                                  aria-label="Elimina sync"
                                >
                                  <Trash2 className="size-4 text-zinc-500 hover:text-red-600" />
                                </Button>
                              </div>
                            </div>

                            {/* avanzamento reale dei file accodati da questa sync */}
                            {jobFiles.length > 0 && (
                              <div className="mt-2.5">
                                <StatusBar rows={jobFiles} />
                              </div>
                            )}

                            {/* da dove sincronizza */}
                            <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-zinc-500 dark:text-zinc-400">
                              <span className="inline-flex items-center gap-1">
                                <Plug className="size-3" />
                                {sourceLabel(j.source_id)}
                              </span>
                              {(j.folders || []).map((f) => (
                                <span
                                  key={f.sharepoint_id}
                                  className="inline-flex items-center gap-1 rounded-full bg-zinc-100 px-2 py-0.5 dark:bg-zinc-800"
                                >
                                  <FolderTree className="size-3" />
                                  {f.name || "cartella"}
                                </span>
                              ))}
                            </div>

                            {/* file saltati + motivo */}
                            {skipped.length > 0 && (
                              <details className="mt-1.5">
                                <summary className="cursor-pointer text-[11px] text-amber-700 dark:text-amber-400">
                                  {skipped.length} file saltati (clic per il dettaglio)
                                </summary>
                                <ul className="mt-1 max-h-32 space-y-0.5 overflow-y-auto pl-1 text-[11px] text-zinc-500 dark:text-zinc-400">
                                  {skipped.map((s, i) => (
                                    <li key={i} className="truncate">
                                      • {s.name} — {s.reason}
                                    </li>
                                  ))}
                                </ul>
                              </details>
                            )}
                          </div>
                        );
                      })}
                    </div>
                    <p className="text-[11px] text-zinc-400 dark:text-zinc-500">
                      L&apos;indicizzazione dei file accodati prosegue qui sotto in “File nella directory”.
                    </p>
                  </div>
                )}
              </>
            )}
          </CardContent>
        </Card>
        )}

        {/* File indicizzati — manuali e sincronizzati tenuti separati */}
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-start justify-between gap-4">
              <div className="flex flex-col gap-1.5">
                <CardTitle className="text-base">File nella directory</CardTitle>
                <CardDescription>
                  {files.length === 0
                    ? "Nessun file."
                    : filtering
                      ? `${shownCount} di ${files.length} mostrati`
                      : `${files.length} file · ${totalManual} caricati a mano · ${totalSynced} da sync`}
                </CardDescription>
              </div>
              {files.length > 0 && (
                <div className="flex shrink-0 items-center gap-0.5 rounded-md border border-zinc-200 p-0.5 text-[11px] dark:border-zinc-800">
                  {(Object.keys(SORT_LABEL) as SortMode[]).map((m) => (
                    <button
                      key={m}
                      onClick={() => setSortMode(m)}
                      className={cn(
                        "rounded px-2 py-1 font-medium transition-colors",
                        sortMode === m
                          ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                          : "text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100",
                      )}
                    >
                      {SORT_LABEL[m]}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {files.length > 0 && (
              <div className="flex flex-col gap-2 pt-3 sm:flex-row sm:items-center sm:justify-between">
                {/* ricerca per nome file */}
                <div className="relative w-full sm:max-w-xs">
                  <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-zinc-400" />
                  <Input
                    value={fileQuery}
                    onChange={(e) => setFileQuery(e.target.value)}
                    placeholder="Cerca per nome…"
                    className="h-8 pl-8 text-xs"
                  />
                </div>
                {/* filtro per stato (solo gli stati presenti) */}
                <div className="flex flex-wrap items-center gap-1">
                  {STATUS_FILTERS.filter(
                    (s) => s.key === "ALL" || (statusCounts[s.key] ?? 0) > 0,
                  ).map((s) => {
                    const n = s.key === "ALL" ? files.length : statusCounts[s.key] ?? 0;
                    return (
                      <button
                        key={s.key}
                        onClick={() => setStatusFilter(s.key)}
                        className={cn(
                          "rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors",
                          statusFilter === s.key
                            ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                            : "bg-zinc-100 text-zinc-600 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700",
                        )}
                      >
                        {s.label} <span className="opacity-60">{n}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </CardHeader>
          <CardContent className="flex flex-col gap-6">
            {/* Action bar selezione massiva: appare quando hai selezionato dei file */}
            {selected.size > 0 && (
              <div className="sticky top-2 z-10 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-indigo-200 bg-indigo-50/90 px-3 py-2 shadow-sm backdrop-blur dark:border-indigo-900/50 dark:bg-indigo-950/40">
                <div className="flex items-center gap-2 text-sm font-medium text-indigo-800 dark:text-indigo-200">
                  <CheckSquare className="size-4" />
                  {selected.size} selezionat{selected.size === 1 ? "o" : "i"}
                  {bulkBusy && (
                    <span className="inline-flex items-center gap-1 text-xs font-normal text-indigo-600 dark:text-indigo-300">
                      <Loader2 className="size-3 animate-spin" />
                      {bulkBusy.action} {bulkBusy.done}/{bulkBusy.total}
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  {files.some((f) => selected.has(f.file_id) && f.status === "FAILED") && (
                    <Button size="sm" variant="outline" disabled={!!bulkBusy} onClick={bulkRetry}>
                      <RotateCcw className="size-4" />
                      Riprova ({files.filter((f) => selected.has(f.file_id) && f.status === "FAILED").length})
                    </Button>
                  )}
                  {files.some((f) => selected.has(f.file_id) && f.status === "EXCLUDED") && (
                    <Button size="sm" variant="outline" disabled={!!bulkBusy} onClick={bulkUnexclude}>
                      <Undo2 className="size-4" />
                      Re-includi ({files.filter((f) => selected.has(f.file_id) && f.status === "EXCLUDED").length})
                    </Button>
                  )}
                  {files.some((f) => selected.has(f.file_id) && f.status !== "EXCLUDED") && (
                    <Button size="sm" variant="outline" disabled={!!bulkBusy} onClick={bulkExclude}>
                      <Ban className="size-4" />
                      Escludi ({files.filter((f) => selected.has(f.file_id) && f.status !== "EXCLUDED").length})
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!!bulkBusy}
                    onClick={bulkDelete}
                    className="text-red-600 hover:text-red-700 dark:text-red-400 dark:hover:text-red-300"
                  >
                    <Trash2 className="size-4" />
                    Elimina ({selected.size})
                  </Button>
                  <Button size="sm" variant="ghost" disabled={!!bulkBusy} onClick={clearSel} aria-label="Deseleziona tutto">
                    <X className="size-4" />
                  </Button>
                </div>
              </div>
            )}
            {files.length === 0 ? (
              <div className="flex h-32 items-center justify-center rounded-md border border-dashed border-zinc-300 text-sm text-zinc-400 dark:border-zinc-800 dark:text-zinc-600">
                Carica il primo file.
              </div>
            ) : shownCount === 0 ? (
              <div className="flex h-24 items-center justify-center rounded-md border border-dashed border-zinc-300 text-sm text-zinc-400 dark:border-zinc-800 dark:text-zinc-600">
                Nessun file corrisponde alla ricerca o al filtro.
              </div>
            ) : (
              <>
                {files.some((f) => f.status === "FAILED") && (
                  <div className="flex items-center justify-between rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
                    <span>
                      {files.filter((f) => f.status === "FAILED").length} file falliti in questa directory.
                    </span>
                    <Button variant="outline" size="sm" onClick={handleRetryAll}>
                      <RotateCcw className="size-4" />
                      Riprova falliti
                    </Button>
                  </div>
                )}
                {manualFiles.length > 0 && (
                  <div className="flex flex-col gap-2">
                    <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                      <CloudUpload className="size-4" />
                      Caricati a mano · {manualFiles.length}
                    </div>
                    <FilesTable rows={manualFiles} selected={selected} onToggle={toggle} onToggleAll={toggleAll} onDelete={handleDelete} onRetry={handleRetry} onExclude={handleExclude} onUnexclude={handleUnexclude} />
                  </div>
                )}

                {syncedFiles.length > 0 && (
                  <div className="flex flex-col gap-2">
                    <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                      <Plug className="size-4" />
                      Sincronizzati · {syncedFiles.length}
                    </div>
                    <p className="text-[11px] text-zinc-400 dark:text-zinc-500">
                      Gestiti dalle sync qui sopra. Eliminandoli a mano tornano al prossimo sync se ancora
                      presenti nella source: per toglierli davvero, elimina la sync.
                    </p>
                    <FilesTable rows={syncedFiles} selected={selected} onToggle={toggle} onToggleAll={toggleAll} onDelete={handleDelete} onRetry={handleRetry} onExclude={handleExclude} onUnexclude={handleUnexclude} />
                  </div>
                )}
              </>
            )}
          </CardContent>
        </Card>

        {/* Dialog: schema di estrazione per una singola sync */}
        <Dialog open={!!schemaSyncId} onOpenChange={(o) => !o && setSchemaSyncId(null)}>
          <DialogHeader>
            <DialogTitle>Estrazione · sync</DialogTitle>
          </DialogHeader>
          <DialogBody>
            {schemaSyncId && (
              <SchemaEditor
                basePath={`/ingest/sharepoint/${schemaSyncId}`}
                levelLabel="sync"
                canReset
              />
            )}
          </DialogBody>
        </Dialog>
      </div>
    </div>
  );
}
