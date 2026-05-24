"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  ChevronRight,
  Database,
  FileText,
  FolderTree,
  Loader2,
  Plus,
  Search,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

export type BrowseFolder = { id: string; name: string; child_count?: number };
type BrowseFile = { id: string; name: string; size?: number };
type Crumb = { driveId: string; folderId: string | null; label: string };
type BrowseResponse = {
  level: "drives" | "folders";
  drives?: BrowseFolder[];
  folders?: BrowseFolder[];
  files?: BrowseFile[];
};

function humanSize(bytes?: number): string {
  if (!bytes) return "";
  const u = ["B", "KB", "MB", "GB"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

type Props = {
  sourceId: string;
  /** ID cartelle già selezionate (mostra "Aggiunta" invece di "Aggiungi"). */
  selectedIds: string[];
  onAdd: (folder: BrowseFolder) => void;
  onClose?: () => void;
};

/**
 * Navigatore di una source: librerie → cartelle → sottocartelle.
 * Mostra le cartelle (navigabili + aggiungibili) e i file del livello corrente
 * (informativi). Auto-carica le librerie al mount.
 */
export function SourceBrowser({ sourceId, selectedIds, onAdd, onClose }: Props) {
  const [level, setLevel] = useState<"drives" | "folders">("drives");
  const [folders, setFolders] = useState<BrowseFolder[]>([]);
  const [files, setFiles] = useState<BrowseFile[]>([]);
  const [crumbs, setCrumbs] = useState<Crumb[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const selected = useMemo(() => new Set(selectedIds), [selectedIds]);

  const load = useCallback(
    async (driveId: string | null, folderId: string | null) => {
      setLoading(true);
      setQuery("");
      setError(null);
      try {
        const qs = new URLSearchParams();
        if (driveId) qs.set("drive_id", driveId);
        if (folderId) qs.set("folder_id", folderId);
        const q = qs.toString();
        const res = await api.get<BrowseResponse>(`/sources/${sourceId}/browse${q ? `?${q}` : ""}`);
        setLevel(res.level);
        setFolders(res.level === "drives" ? res.drives || [] : res.folders || []);
        setFiles(res.files || []);
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    },
    [sourceId],
  );

  // (ri)parti dalle librerie quando cambia la source
  useEffect(() => {
    setCrumbs([]);
    load(null, null);
  }, [load]);

  function enterDrive(d: BrowseFolder) {
    setCrumbs([{ driveId: d.id, folderId: null, label: d.name }]);
    load(d.id, null);
  }
  function enterFolder(f: BrowseFolder) {
    const last = crumbs[crumbs.length - 1];
    if (!last) return;
    setCrumbs([...crumbs, { driveId: last.driveId, folderId: f.id, label: f.name }]);
    load(last.driveId, f.id);
  }
  function gotoCrumb(i: number) {
    if (i < 0) {
      setCrumbs([]);
      load(null, null);
      return;
    }
    const c = crumbs.slice(0, i + 1);
    setCrumbs(c);
    load(c[i].driveId, c[i].folderId);
  }

  const isDrives = level === "drives";
  const ql = query.trim().toLowerCase();
  const visibleFolders = folders.filter((f) => f.name.toLowerCase().includes(ql));
  const visibleFiles = files.filter((f) => f.name.toLowerCase().includes(ql));

  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800">
      {/* breadcrumb */}
      <div className="flex items-center gap-1 border-b border-zinc-200 px-3 py-2 text-xs dark:border-zinc-800">
        <button onClick={() => gotoCrumb(-1)} className="text-zinc-500 hover:text-indigo-600">
          Librerie
        </button>
        {crumbs.map((c, i) => (
          <span key={i} className="inline-flex items-center gap-1">
            <ChevronRight className="size-3 text-zinc-300 dark:text-zinc-600" />
            <button onClick={() => gotoCrumb(i)} className="text-zinc-500 hover:text-indigo-600">
              {c.label}
            </button>
          </span>
        ))}
        {onClose && (
          <Button variant="ghost" size="icon" className="ml-auto" onClick={onClose} aria-label="Chiudi">
            <X className="size-4" />
          </Button>
        )}
      </div>

      {/* hint + ricerca */}
      <div className="flex flex-col gap-1.5 border-b border-zinc-200 px-3 py-2 dark:border-zinc-800">
        <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
          {isDrives
            ? "Scegli una libreria, poi entra nelle cartelle e aggiungile alla sync."
            : "Clicca una cartella per entrarci · “Aggiungi” per includerla nella sync."}
        </p>
        {!loading && (folders.length > 0 || files.length > 0) && (
          <p className="text-[11px] font-medium text-zinc-600 dark:text-zinc-300">
            {isDrives
              ? `${folders.length} librerie`
              : `${folders.length} cartelle · ${files.length} file · ${
                  folders.length + files.length
                } elementi`}
          </p>
        )}
        {(folders.length > 0 || files.length > 0) && (
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-zinc-400" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filtra per nome…"
              className="h-8 pl-7 text-xs"
            />
          </div>
        )}
      </div>

      {error && (
        <div className="border-b border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
          {error}
        </div>
      )}

      <div className="max-h-72 overflow-y-auto p-2">
        {loading ? (
          <div className="flex items-center justify-center py-8 text-zinc-400">
            <Loader2 className="size-5 animate-spin" />
          </div>
        ) : folders.length === 0 && files.length === 0 ? (
          <div className="py-8 text-center text-sm text-zinc-400">
            {isDrives ? "Nessuna libreria." : "Cartella vuota."}
          </div>
        ) : (
          <div className="flex flex-col gap-0.5">
            {/* CARTELLE / LIBRERIE — cliccabili per entrare */}
            {visibleFolders.map((f) => {
              const added = selected.has(f.id);
              return (
                <div
                  key={f.id}
                  className="group flex items-center gap-2 rounded-md hover:bg-zinc-100 dark:hover:bg-zinc-800/60"
                >
                  <button
                    onClick={() => (isDrives ? enterDrive(f) : enterFolder(f))}
                    className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 px-2 py-2 text-left text-sm"
                  >
                    {isDrives ? (
                      <Database className="size-4 shrink-0 text-zinc-400" />
                    ) : (
                      <FolderTree className="size-4 shrink-0 text-indigo-500" />
                    )}
                    <span className="truncate">{f.name}</span>
                    {typeof f.child_count === "number" && f.child_count > 0 && (
                      <span className="shrink-0 text-[11px] text-zinc-400">{f.child_count} elem.</span>
                    )}
                    <ChevronRight className="ml-auto size-4 shrink-0 text-zinc-300 group-hover:text-zinc-500 dark:text-zinc-600" />
                  </button>
                  {!isDrives &&
                    (added ? (
                      <span className="mr-1 inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
                        <Check className="size-3" />
                        Aggiunta
                      </span>
                    ) : (
                      <Button variant="ghost" size="sm" className="mr-1" onClick={() => onAdd(f)}>
                        <Plus className="size-4" />
                        Aggiungi
                      </Button>
                    ))}
                </div>
              );
            })}

            {/* FILE del livello corrente — informativi (non cliccabili) */}
            {!isDrives && visibleFiles.length > 0 && (
              <div className="mt-1 border-t border-zinc-100 pt-1 dark:border-zinc-800">
                <p className="px-2 py-1 text-[11px] uppercase tracking-wide text-zinc-400">
                  {visibleFiles.length} file in questa cartella
                </p>
                {visibleFiles.map((f) => (
                  <div
                    key={f.id}
                    className="flex items-center gap-2 px-2 py-1.5 text-sm text-zinc-500 dark:text-zinc-400"
                  >
                    <FileText className="size-4 shrink-0 text-zinc-300 dark:text-zinc-600" />
                    <span className="truncate">{f.name}</span>
                    {!!f.size && <span className="ml-auto shrink-0 text-[11px]">{humanSize(f.size)}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
