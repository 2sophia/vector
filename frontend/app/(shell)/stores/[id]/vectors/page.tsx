"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  BarChart3,
  Boxes,
  Copy,
  Database,
  FileWarning,
  FolderTree,
  Layers,
  RefreshCw,
  Share2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

// Palette dedicata alle categorie (ciclica per posizione): le barre devono avere
// colori distinti — NODE_COLORS del grafo è keyed per TIPO-NODO, non per categoria.
const CAT_PALETTE = ["#6366f1", "#3b82f6", "#06b6d4", "#22c55e", "#f59e0b", "#f97316", "#a855f7", "#ec4899"];

type VectorsStats = {
  vector_store_id: string;
  cached: boolean;
  computed_at: number;
  counts: {
    points: number;
    files: { total: number; completed: number; in_progress: number; failed: number };
    directories: number;
    categories: number;
  };
  by_directory: { slug: string; name: string; points: number; files: number }[];
  by_category: { label: string; points: number }[];
  semantic_clusters: {
    id: number;
    label: string;
    top_heading?: string | null;
    size: number;
    doc_count: number;
    top_files: { filename: string; chunks: number }[];
  }[];
  near_duplicates: { points: number; clusters: number; redundant: number; reduction_pct: number };
  graph: {
    graph_enabled: boolean;
    documents?: number;
    chunks?: number;
    entities?: number;
    mentions?: number;
    relations?: number;
  };
};

function Stat({
  icon,
  label,
  value,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  tone?: "amber" | "default";
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-1 rounded-lg border p-3",
        tone === "amber"
          ? "border-amber-200 bg-amber-50 dark:border-amber-900/50 dark:bg-amber-950/20"
          : "border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900",
      )}
    >
      <div className="flex items-center gap-1.5 text-xs text-zinc-500 dark:text-zinc-400">
        {icon}
        {label}
      </div>
      <div className="text-2xl font-semibold tracking-tight">{value}</div>
    </div>
  );
}

export default function StoreVectorsPage() {
  const vsid = useParams<{ id: string }>().id;

  const [storeName, setStoreName] = useState("");
  const [data, setData] = useState<VectorsStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const s = await api.get<{ name: string }>(`/vector_stores/${vsid}`);
        setStoreName(s.name || vsid);
      } catch {
        setStoreName(vsid);
      }
    })();
  }, [vsid]);

  const load = useCallback(
    async (force = false) => {
      setLoading(true);
      setError(null);
      try {
        const res = await api.get<VectorsStats>(
          `/vector_stores/${vsid}/overview${force ? "?refresh=true" : ""}`,
        );
        setData(res);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Errore di caricamento");
      } finally {
        setLoading(false);
      }
    },
    [vsid],
  );

  useEffect(() => {
    load();
  }, [load]);

  const maxCat = Math.max(1, ...(data?.by_category ?? []).map((c) => c.points));

  return (
    <div className="px-8 py-10">
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        {/* header */}
        <div className="flex items-end justify-between gap-4">
          <div className="flex flex-col gap-1">
            <Link
              href={`/stores/${vsid}`}
              className="inline-flex items-center gap-1.5 text-sm text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
            >
              <ArrowLeft className="size-4" />
              {storeName || "…"}
            </Link>
            <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
              <BarChart3 className="size-6 text-indigo-500" />
              Vectors — quadro d&apos;insieme
            </h1>
            {data && (
              <p className="text-sm text-zinc-500 dark:text-zinc-400">
                Calcolato il {new Date(data.computed_at * 1000).toLocaleString("it-IT")}
                {data.cached ? " · da cache" : " · ricalcolato"}
              </p>
            )}
          </div>
          <Button variant="outline" size="sm" onClick={() => load(true)} disabled={loading}>
            <RefreshCw className={cn("size-4", loading && "animate-spin")} />
            Aggiorna
          </Button>
        </div>

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
            {error}
          </div>
        )}

        {loading && !data ? (
          <div className="flex h-40 items-center justify-center text-sm text-zinc-500">
            Calcolo del quadro d&apos;insieme…
          </div>
        ) : data ? (
          <>
            {/* KPI */}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              <Stat icon={<Database className="size-3.5" />} label="Punti" value={data.counts.points.toLocaleString("it-IT")} />
              <Stat icon={<Boxes className="size-3.5" />} label="File indicizzati" value={data.counts.files.completed} />
              <Stat
                icon={<FileWarning className="size-3.5" />}
                label="Falliti"
                value={data.counts.files.failed}
                tone={data.counts.files.failed > 0 ? "amber" : "default"}
              />
              <Stat icon={<FolderTree className="size-3.5" />} label="Directory" value={data.counts.directories} />
              <Stat icon={<Layers className="size-3.5" />} label="Categorie" value={data.counts.categories} />
              <Stat
                icon={<Copy className="size-3.5" />}
                label="Near-duplicate"
                value={`${data.near_duplicates.reduction_pct ?? 0}%`}
                tone={(data.near_duplicates.redundant ?? 0) > 0 ? "amber" : "default"}
              />
            </div>

            {/* distribuzione per categoria */}
            <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
              <h2 className="mb-3 text-sm font-semibold">Distribuzione per categoria</h2>
              {data.by_category.length === 0 ? (
                <p className="text-sm text-zinc-400">
                  Nessuna categoria (il classifier è disattivo o non ha prodotto tag).
                </p>
              ) : (
                <div className="flex flex-col gap-2">
                  {data.by_category.map((c, i) => (
                    <div key={c.label} className="flex items-center gap-3 text-sm">
                      <span className="w-48 shrink-0 truncate text-zinc-600 dark:text-zinc-300" title={c.label}>
                        {c.label}
                      </span>
                      <div className="h-2 flex-1 overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800">
                        <div
                          className="h-full rounded-full"
                          style={{
                            width: `${(c.points / maxCat) * 100}%`,
                            backgroundColor: CAT_PALETTE[i % CAT_PALETTE.length],
                          }}
                        />
                      </div>
                      <span className="w-14 shrink-0 text-right tabular-nums text-zinc-500">{c.points}</span>
                    </div>
                  ))}
                </div>
              )}
            </section>

            {/* cluster semantici */}
            <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
              <h2 className="mb-1 text-sm font-semibold">Gruppi semantici</h2>
              <p className="mb-3 text-xs text-zinc-400">
                Raggruppamento <strong>automatico</strong> dei frammenti per similarità degli
                embedding (K-Means sui vettori dense, non supervisionato). L&apos;etichetta è il{" "}
                <em>tema dominante</em> del gruppo. I gruppi sono <strong>trasversali</strong> alle
                categorie: il numero di frammenti di un gruppo <strong>non</strong> coincide col
                totale della categoria omonima qui sopra (una categoria si spalma su più gruppi, e
                un frammento può avere più categorie).
              </p>
              {data.semantic_clusters.length === 0 ? (
                <p className="text-sm text-zinc-400">Troppi pochi punti per individuare gruppi.</p>
              ) : (
                <div className="divide-y divide-zinc-100 dark:divide-zinc-800">
                  {data.semantic_clusters.map((cl) => (
                    <div key={cl.id} className="flex items-start gap-3 py-2">
                      <span
                        className="mt-0.5 inline-flex shrink-0 items-center rounded-full bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300"
                        title={`${cl.size} frammenti`}
                      >
                        {cl.size}
                      </span>
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium" title={cl.label}>
                          {cl.label}
                        </div>
                        {cl.top_heading && cl.top_heading !== cl.label && (
                          <div className="truncate text-[11px] text-zinc-500" title={cl.top_heading}>
                            {cl.top_heading}
                          </div>
                        )}
                        {cl.top_files?.length > 0 && (
                          <div className="mt-0.5 text-[11px] text-zinc-400">
                            da {cl.doc_count} {cl.doc_count === 1 ? "documento" : "documenti"} ·{" "}
                            <span className="font-mono" title={cl.top_files.map((f) => `${f.filename} (${f.chunks})`).join("\n")}>
                              {cl.top_files.map((f) => f.filename).join(", ")}
                            </span>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            {/* per directory + knowledge graph */}
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {data.by_directory.length > 0 && (
                <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                  <h2 className="mb-3 text-sm font-semibold">Per directory</h2>
                  <div className="divide-y divide-zinc-100 dark:divide-zinc-800">
                    {data.by_directory.map((d) => (
                      <div key={d.slug} className="flex items-center justify-between py-1.5 text-sm">
                        <span className="truncate text-zinc-600 dark:text-zinc-300" title={d.name}>
                          {d.name}
                        </span>
                        <span className="shrink-0 text-xs text-zinc-500">
                          {d.files} file · {d.points} punti
                        </span>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                <div className="mb-3 flex items-center justify-between">
                  <h2 className="text-sm font-semibold">Knowledge graph</h2>
                  <Link
                    href={`/stores/${vsid}/graph`}
                    className="inline-flex items-center gap-1 text-xs text-indigo-600 hover:underline dark:text-indigo-400"
                  >
                    <Share2 className="size-3.5" />
                    Apri grafo
                  </Link>
                </div>
                {data.graph.graph_enabled ? (
                  <div className="grid grid-cols-3 gap-3">
                    <Stat icon={<Layers className="size-3.5" />} label="Entità" value={data.graph.entities ?? 0} />
                    <Stat icon={<Share2 className="size-3.5" />} label="Menzioni" value={data.graph.mentions ?? 0} />
                    <Stat icon={<Share2 className="size-3.5" />} label="Relazioni" value={data.graph.relations ?? 0} />
                  </div>
                ) : (
                  <p className="text-sm text-zinc-400">Knowledge graph disattivato per questo deployment.</p>
                )}
              </section>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
