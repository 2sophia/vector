"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import dynamic from "next/dynamic";
import { ArrowLeft, Box, RefreshCw, Search, Share2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { GraphData, GraphNode } from "@/components/graph-viewer";
import { NODE_COLORS } from "@/components/graph-viewer";

// force-graph + three usano window/WebGL → client-only
const GraphViewer = dynamic(
  () => import("@/components/graph-viewer").then((m) => m.GraphViewer),
  { ssr: false },
);

type GraphResponse = GraphData & {
  metadata?: { node_count?: number; edge_count?: number; truncated?: boolean; graph_enabled?: boolean };
};

export default function StoreGraphPage() {
  const params = useParams<{ id: string }>();
  const vectorStoreId = params.id;

  const [storeName, setStoreName] = useState("");
  const [data, setData] = useState<GraphResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [view3D, setView3D] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");

  // dimensioni del canvas (force-graph vuole width/height espliciti)
  const wrapRef = useRef<HTMLDivElement>(null);
  const [dims, setDims] = useState({ w: 800, h: 600 });

  useEffect(() => {
    (async () => {
      try {
        const s = await api.get<{ name: string }>(`/vector_stores/${vectorStoreId}`);
        setStoreName(s.name || vectorStoreId);
      } catch {
        setStoreName(vectorStoreId);
      }
    })();
  }, [vectorStoreId]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get<GraphResponse>(`/vector_stores/${vectorStoreId}/graph`);
      setData(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Errore di caricamento");
    } finally {
      setLoading(false);
    }
  }, [vectorStoreId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setDims({ w: el.clientWidth, h: el.clientHeight });
    });
    ro.observe(el);
    setDims({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, [loading]);

  // label presenti nel grafo + conteggio
  const labelCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const n of data?.nodes ?? []) m.set(n.label, (m.get(n.label) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [data]);

  const visibleLabels = useMemo(() => {
    const all = new Set(labelCounts.map(([l]) => l));
    for (const h of hidden) all.delete(h);
    return all;
  }, [labelCounts, hidden]);

  function toggleLabel(label: string) {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  }

  function runSearch() {
    const q = query.trim().toLowerCase();
    if (!q || !data) return;
    const hit = data.nodes.find(
      (n) => n.name.toLowerCase().includes(q) || n.id.toLowerCase().includes(q),
    );
    if (hit) setSelectedId(hit.id);
  }

  const selectedNode: GraphNode | null = useMemo(
    () => data?.nodes.find((n) => n.id === selectedId) ?? null,
    [data, selectedId],
  );

  const meta = data?.metadata;

  return (
    <div className="flex h-[calc(100vh-2rem)] flex-col gap-3 px-6 py-5">
      {/* header */}
      <div className="flex items-end justify-between gap-4">
        <div className="flex flex-col gap-1">
          <Link
            href={`/stores/${vectorStoreId}`}
            className="inline-flex items-center gap-1.5 text-sm text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
          >
            <ArrowLeft className="size-4" />
            {storeName || "…"}
          </Link>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <Share2 className="size-5 text-indigo-500" />
            Knowledge Graph
            {meta && (
              <span className="text-sm font-normal text-zinc-500">
                · {meta.node_count} nodi · {meta.edge_count} archi
                {meta.truncated && " (troncato)"}
              </span>
            )}
          </h1>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => setView3D((v) => !v)}>
            <Box className="size-4" />
            {view3D ? "Vista 2D" : "Vista 3D"}
          </Button>
          <Button variant="outline" size="sm" onClick={load} disabled={loading}>
            <RefreshCw className={cn("size-4", loading && "animate-spin")} />
            Refresh
          </Button>
        </div>
      </div>

      {/* toolbar: ricerca + legenda */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-1.5">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-zinc-400" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && runSearch()}
              placeholder="Cerca un nodo…"
              className="h-8 w-56 pl-8 text-sm"
            />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {labelCounts.map(([label, count]) => {
            const off = hidden.has(label);
            return (
              <button
                key={label}
                onClick={() => toggleLabel(label)}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs transition-opacity",
                  off ? "opacity-40" : "opacity-100",
                  "border-zinc-200 dark:border-zinc-700",
                )}
                title={off ? "Mostra" : "Nascondi"}
              >
                <span
                  className="size-2.5 rounded-full"
                  style={{ backgroundColor: NODE_COLORS[label] ?? "#94a3b8" }}
                />
                {label} <span className="text-zinc-400">{count}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* area grafo */}
      <div
        ref={wrapRef}
        className="relative flex-1 overflow-hidden rounded-lg border border-zinc-800 bg-[#09090b]"
      >
        {loading ? (
          <div className="flex h-full items-center justify-center text-sm text-zinc-500">
            Caricamento grafo…
          </div>
        ) : error ? (
          <div className="flex h-full items-center justify-center text-sm text-red-600">{error}</div>
        ) : !data || data.nodes.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-sm text-zinc-500">
            <Share2 className="size-8 text-zinc-300 dark:text-zinc-700" />
            Nessun nodo nel grafo. Fai l&apos;ingest di qualche documento, poi torna qui.
          </div>
        ) : (
          <GraphViewer
            graphData={data}
            width={dims.w}
            height={dims.h}
            view3D={view3D}
            selectedNode={selectedId}
            visibleLabels={visibleLabels}
            onNodeClick={(n) => setSelectedId(n?.id ?? null)}
          />
        )}

        {/* pannello dettaglio nodo */}
        {selectedNode && (
          <div className="absolute right-3 top-3 z-20 w-72 rounded-lg border border-zinc-200 bg-white/95 p-3 shadow-lg backdrop-blur dark:border-zinc-700 dark:bg-zinc-900/95">
            <div className="mb-2 flex items-start justify-between gap-2">
              <span
                className="rounded-full px-2 py-0.5 text-[11px] font-medium text-white"
                style={{ backgroundColor: NODE_COLORS[selectedNode.label] ?? "#94a3b8" }}
              >
                {selectedNode.label}
              </span>
              <button onClick={() => setSelectedId(null)} aria-label="Chiudi">
                <X className="size-4 text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200" />
              </button>
            </div>
            <p className="mb-2 break-words text-sm font-medium">{selectedNode.name}</p>
            <div className="max-h-64 space-y-1 overflow-auto">
              {Object.entries(selectedNode.properties ?? {}).map(([k, v]) => (
                <div key={k} className="text-[11px]">
                  <span className="font-mono text-zinc-400">{k}</span>:{" "}
                  <span className="break-words text-zinc-700 dark:text-zinc-300">{String(v)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
