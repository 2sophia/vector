"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import {
  Search as SearchIcon,
  Loader2,
  Network,
  Database,
  ArrowRight,
  Code2,
  LayoutList,
  ChevronDown,
  SlidersHorizontal,
  Sparkles,
  Share2,
  Box,
  Copy,
  Check,
  Download,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { GraphData } from "@/components/graph-viewer";
import { NODE_COLORS } from "@/components/graph-viewer";

// force-graph + three: client-only
const GraphViewer = dynamic(
  () => import("@/components/graph-viewer").then((m) => m.GraphViewer),
  { ssr: false },
);

type VectorStore = { id: string; name: string };
type SearchResult = {
  id: string;
  score: number | null;
  score_qdrant: number | null;
  filename: string | null;
  file_id: string | null;
  content: string | null;
  distance?: number;
  attributes?: Record<string, unknown>;
};
type SearchResponse = {
  object: string;
  data: SearchResult[];
  query: string;
  usage?: Record<string, unknown>;
};
type SubGraph = GraphData & {
  seed_ids?: string[];
  metadata?: { node_count?: number; edge_count?: number; query_time_ms?: number };
};

// Provenienza di un risultato (campo _source iniettato da M4)
const SOURCE_META: Record<string, { label: string; cls: string; icon: typeof Database }> = {
  qdrant: {
    label: "vettoriale",
    cls: "bg-sky-100 text-sky-800 dark:bg-sky-950/60 dark:text-sky-300",
    icon: Database,
  },
  "graph:mentions": {
    label: "grafo · entità",
    cls: "bg-violet-100 text-violet-800 dark:bg-violet-950/60 dark:text-violet-300",
    icon: Network,
  },
  "graph:next": {
    label: "grafo · contesto",
    cls: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300",
    icon: ArrowRight,
  },
};

function sourceMeta(src: unknown) {
  return SOURCE_META[String(src)] || SOURCE_META["qdrant"];
}

// Syntax highlight leggero per JSON (niente librerie): colora chiavi, stringhe,
// numeri, booleani e null. L'input è già escapato prima dell'iniezione.
function highlightJson(value: unknown): string {
  const json = JSON.stringify(value, null, 2)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return json.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = "text-emerald-300"; // number
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? "text-sky-300" : "text-amber-200"; // key : value
      } else if (/true|false/.test(match)) {
        cls = "text-violet-300";
      } else if (/null/.test(match)) {
        cls = "text-zinc-500";
      }
      return `<span class="${cls}">${match}</span>`;
    },
  );
}

export default function SearchPage() {
  const [stores, setStores] = useState<VectorStore[]>([]);
  const [storeId, setStoreId] = useState("");
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(10);

  // M4 / opzioni avanzate
  const [graphExpand, setGraphExpand] = useState(true);
  const [neighbors, setNeighbors] = useState(20);
  const [dfMax, setDfMax] = useState(0.5);
  const [slug, setSlug] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);

  const [results, setResults] = useState<SearchResult[]>([]);
  const [raw, setRaw] = useState<SearchResponse | null>(null);
  const [view, setView] = useState<"cards" | "json" | "graph">("cards");
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);

  // search-as-graph (vista "graph")
  const [graphData, setGraphData] = useState<SubGraph | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [view3D, setView3D] = useState(false);
  const [graphSelected, setGraphSelected] = useState<string | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [dims, setDims] = useState({ w: 800, h: 560 });

  useEffect(() => {
    (async () => {
      try {
        const res = await api.get<{ data: VectorStore[] }>("/vector_stores");
        setStores(res.data || []);
        if (res.data?.length) setStoreId(res.data[0].id);
      } catch (e) {
        setError(String(e));
      }
    })();
  }, []);

  async function handleSearch() {
    if (!storeId || !query.trim()) return;
    setSearching(true);
    setError(null);
    setGraphData(null); // invalida il grafo: si ricarica alla prossima vista "graph"
    setGraphSelected(null);
    const t0 = performance.now();
    try {
      const body: Record<string, unknown> = {
        query: query.trim(),
        max_num_results: topK,
        graph_expand: graphExpand,
        graph_neighbors: neighbors,
        graph_df_max: dfMax,
      };
      if (slug.trim()) body.filters = { sophia_directory_slug: slug.trim() };
      const res = await api.post<SearchResponse>(`/vector_stores/${storeId}/search`, body);
      setResults(res.data || []);
      setRaw(res);
      setElapsed(Math.round(performance.now() - t0));
    } catch (e) {
      setError(String(e));
      setResults([]);
      setRaw(null);
    } finally {
      setSearching(false);
    }
  }

  // carica il sottografo dei risultati (search-as-graph) — lazy alla prima vista "graph"
  const loadGraph = useCallback(async () => {
    if (!storeId || !query.trim()) return;
    setGraphLoading(true);
    try {
      const body: Record<string, unknown> = {
        query: query.trim(),
        max_num_results: topK,
        graph_expand: graphExpand,
        graph_neighbors: neighbors,
        graph_df_max: dfMax,
      };
      if (slug.trim()) body.filters = { sophia_directory_slug: slug.trim() };
      const res = await api.post<{ graph: SubGraph }>(
        `/vector_stores/${storeId}/search/graph`,
        body,
      );
      setGraphData(res.graph);
    } catch (e) {
      setError(String(e));
    } finally {
      setGraphLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storeId, query, topK, graphExpand, neighbors, dfMax, slug]);

  // alla prima apertura della vista grafo (per la query corrente) carica il sottografo
  useEffect(() => {
    if (view === "graph" && raw && !graphData && !graphLoading) loadGraph();
  }, [view, raw, graphData, graphLoading, loadGraph]);

  // dimensioni del canvas force-graph
  useEffect(() => {
    if (view !== "graph") return;
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setDims({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setDims({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, [view, graphData]);

  const seedSet = useMemo(() => new Set(graphData?.seed_ids ?? []), [graphData]);

  async function copyJson() {
    if (!raw) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(raw, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard non disponibile */
    }
  }

  function downloadJson() {
    if (!raw) return;
    const blob = new Blob([JSON.stringify(raw, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `search-${storeId || "results"}-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  // conteggio per provenienza
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const r of results) {
      const s = String(r.attributes?._source ?? "qdrant");
      c[s] = (c[s] || 0) + 1;
    }
    return c;
  }, [results]);

  return (
    <div className="px-8 py-10">
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <div className="flex flex-col gap-1.5">
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <SearchIcon className="size-6 text-indigo-500" />
            Search playground
          </h1>
          <p className="text-sm text-zinc-500 dark:text-zinc-400">
            Hybrid dense+sparse + rerank, con <span className="font-medium text-violet-600 dark:text-violet-400">graph-augmented retrieval</span>:
            i risultati vengono espansi nel knowledge graph (entità condivise + contesto) e ri-rankizzati.
          </p>
        </div>

        {/* ===== CONTROLLI ===== */}
        <Card>
          <CardContent className="flex flex-col gap-4 pt-6">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-[1fr_7rem]">
              <div className="flex flex-col gap-2">
                <Label htmlFor="search-store">Vector store</Label>
                <Select value={storeId} onValueChange={setStoreId} disabled={stores.length === 0}>
                  <SelectTrigger id="search-store">
                    <SelectValue placeholder="Nessuno store" />
                  </SelectTrigger>
                  <SelectContent>
                    {stores.map((s) => (
                      <SelectItem key={s.id} value={s.id}>{s.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="search-topk">Risultati</Label>
                <Input
                  id="search-topk"
                  type="number"
                  min={1}
                  max={50}
                  value={topK}
                  onChange={(e) => setTopK(Number(e.target.value) || 10)}
                />
              </div>
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="search-query">Query</Label>
              <div className="flex gap-2">
                <Input
                  id="search-query"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="es. obblighi di trasparenza verso la clientela"
                  onKeyDown={(e) => e.key === "Enter" && handleSearch()}
                />
                <Button onClick={handleSearch} disabled={searching || !storeId || !query.trim()}>
                  {searching ? <Loader2 className="size-4 animate-spin" /> : <SearchIcon className="size-4" />}
                  Cerca
                </Button>
              </div>
            </div>

            {/* riga toggle graph + avanzate */}
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={() => setGraphExpand((v) => !v)}
                className={cn(
                  "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors",
                  graphExpand
                    ? "border-violet-300 bg-violet-50 text-violet-700 dark:border-violet-800 dark:bg-violet-950/50 dark:text-violet-300"
                    : "border-zinc-200 text-zinc-500 dark:border-zinc-800 dark:text-zinc-400",
                )}
              >
                <span
                  className={cn(
                    "flex h-4 w-7 items-center rounded-full px-0.5 transition-colors",
                    graphExpand ? "bg-violet-500" : "bg-zinc-300 dark:bg-zinc-700",
                  )}
                >
                  <span
                    className={cn(
                      "size-3 rounded-full bg-white transition-transform",
                      graphExpand && "translate-x-3",
                    )}
                  />
                </span>
                <Network className="size-3.5" />
                Graph-augmented
              </button>

              <button
                type="button"
                onClick={() => setShowAdvanced((v) => !v)}
                className="inline-flex items-center gap-1.5 text-xs text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
              >
                <SlidersHorizontal className="size-3.5" />
                Opzioni avanzate
                <ChevronDown className={cn("size-3.5 transition-transform", showAdvanced && "rotate-180")} />
              </button>
            </div>

            {showAdvanced && (
              <div className="grid grid-cols-2 gap-4 rounded-lg border border-zinc-200 p-3 sm:grid-cols-3 dark:border-zinc-800">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="adv-neighbors" className="text-xs">Vicini grafo (max)</Label>
                  <Input id="adv-neighbors" type="number" min={0} max={100} value={neighbors}
                    onChange={(e) => setNeighbors(Number(e.target.value) || 0)} disabled={!graphExpand} />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="adv-df" className="text-xs">Frequenza max entità (0-1)</Label>
                  <Input id="adv-df" type="number" min={0.1} max={1} step={0.05} value={dfMax}
                    onChange={(e) => setDfMax(Number(e.target.value) || 0.5)} disabled={!graphExpand} />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="adv-slug" className="text-xs">Filtro directory (slug)</Label>
                  <Input id="adv-slug" value={slug} placeholder="(tutte)"
                    onChange={(e) => setSlug(e.target.value)} />
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
            {error}
          </div>
        )}

        {/* ===== BARRA RISULTATI ===== */}
        {raw && (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
              <span className="font-medium text-zinc-700 dark:text-zinc-200">{results.length} risultati</span>
              {Object.entries(counts).map(([src, n]) => {
                const m = sourceMeta(src);
                const Icon = m.icon;
                return (
                  <span key={src} className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium", m.cls)}>
                    <Icon className="size-3" />
                    {n} {m.label}
                  </span>
                );
              })}
              {elapsed !== null && <span>· {elapsed}ms</span>}
            </div>
            {/* switch vista cards/json */}
            <div className="inline-flex rounded-lg border border-zinc-200 p-0.5 dark:border-zinc-800">
              {(["cards", "json", "graph"] as const).map((v) => {
                const Icon = v === "cards" ? LayoutList : v === "json" ? Code2 : Share2;
                const label = v === "cards" ? "Cards" : v === "json" ? "JSON" : "Graph";
                return (
                  <button
                    key={v}
                    onClick={() => setView(v)}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                      view === v
                        ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                        : "text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200",
                    )}
                  >
                    <Icon className="size-3.5" />
                    {label}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* ===== JSON VIEW ===== */}
        {raw && view === "json" && (
          <div className="relative">
            <div className="absolute right-2 top-2 z-10 flex gap-1">
              <button
                onClick={copyJson}
                className="inline-flex items-center gap-1 rounded-md border border-white/10 bg-white/10 px-2 py-1 text-[11px] font-medium text-zinc-200 backdrop-blur transition-colors hover:bg-white/20"
              >
                {copied ? <Check className="size-3 text-emerald-400" /> : <Copy className="size-3" />}
                {copied ? "Copiato" : "Copia"}
              </button>
              <button
                onClick={downloadJson}
                className="inline-flex items-center gap-1 rounded-md border border-white/10 bg-white/10 px-2 py-1 text-[11px] font-medium text-zinc-200 backdrop-blur transition-colors hover:bg-white/20"
              >
                <Download className="size-3" />
                Scarica
              </button>
            </div>
            <pre
              className="scrollbar-hide max-h-[70vh] overflow-auto rounded-lg border border-zinc-200 bg-zinc-950 p-4 font-mono text-[11px] leading-relaxed text-zinc-100 dark:border-zinc-800"
              dangerouslySetInnerHTML={{ __html: highlightJson(raw) }}
            />
          </div>
        )}

        {/* ===== CARDS VIEW ===== */}
        {raw && view === "cards" && (
          <div className="flex flex-col gap-3">
            {results.map((r, i) => {
              const src = String(r.attributes?._source ?? "qdrant");
              const m = sourceMeta(src);
              const Icon = m.icon;
              const via = (r.attributes?._via as string[] | undefined) || [];
              return (
                <Card key={r.id || i}>
                  <CardContent className="flex flex-col gap-2.5 pt-5">
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="shrink-0 font-mono text-xs text-zinc-400">#{i + 1}</span>
                        <span className="truncate text-sm font-medium">{r.filename || "—"}</span>
                      </div>
                      <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5 text-[11px]">
                        <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-medium", m.cls)}>
                          <Icon className="size-3" />
                          {m.label}
                        </span>
                        {r.score !== null && (
                          <span className="rounded-full bg-indigo-100 px-2 py-0.5 font-medium text-indigo-800 dark:bg-indigo-950 dark:text-indigo-300">
                            rerank {r.score.toFixed(3)}
                          </span>
                        )}
                        {r.score_qdrant !== null && (
                          <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
                            qdrant {r.score_qdrant.toFixed(3)}
                          </span>
                        )}
                      </div>
                    </div>

                    {/* entità-ponte (solo per i risultati dal grafo) */}
                    {via.length > 0 && (
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Sparkles className="size-3 text-violet-400" />
                        {via.map((e, j) => (
                          <span key={j} className="rounded-full bg-violet-50 px-2 py-0.5 text-[11px] text-violet-700 dark:bg-violet-950/40 dark:text-violet-300">
                            {e}
                          </span>
                        ))}
                      </div>
                    )}

                    <p className="whitespace-pre-wrap text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
                      {r.content || "(nessun testo)"}
                    </p>

                    <div className="flex items-center gap-3 font-mono text-[10px] text-zinc-400 dark:text-zinc-600">
                      <span>{r.file_id}</span>
                      {typeof r.distance === "number" && <span>dist {r.distance.toFixed(3)}</span>}
                    </div>
                  </CardContent>
                </Card>
              );
            })}
          </div>
        )}

        {/* ===== GRAPH VIEW (search-as-graph) ===== */}
        {raw && view === "graph" && (
          <div
            ref={wrapRef}
            className="relative h-[70vh] overflow-hidden rounded-lg border border-zinc-800 bg-[#09090b]"
          >
            {/* toolbar: 2D/3D + meta + legenda */}
            <div className="absolute left-3 top-3 z-20 flex flex-wrap items-center gap-2">
              <button
                onClick={() => setView3D((v) => !v)}
                className="inline-flex items-center gap-1.5 rounded-md border border-white/15 bg-white/10 px-2 py-1 text-xs font-medium text-zinc-100 backdrop-blur hover:bg-white/20"
              >
                <Box className="size-3.5" />
                {view3D ? "Vista 2D" : "Vista 3D"}
              </button>
              {graphData?.metadata && (
                <span className="rounded-md bg-white/10 px-2 py-1 text-[11px] text-zinc-300 backdrop-blur">
                  {graphData.metadata.node_count} nodi · {graphData.metadata.edge_count} archi ·{" "}
                  {seedSet.size} risultati
                </span>
              )}
            </div>
            {/* legenda */}
            <div className="absolute bottom-3 left-3 z-20 flex flex-wrap gap-2">
              {["Document", "Chunk", "Entity"].map((l) => (
                <span key={l} className="inline-flex items-center gap-1.5 rounded-md bg-white/10 px-2 py-0.5 text-[11px] text-zinc-200 backdrop-blur">
                  <span className="size-2.5 rounded-full" style={{ backgroundColor: NODE_COLORS[l] }} />
                  {l}
                </span>
              ))}
            </div>

            {graphLoading ? (
              <div className="flex h-full items-center justify-center gap-2 text-sm text-zinc-400">
                <Loader2 className="size-4 animate-spin" /> Ricostruzione del grafo…
              </div>
            ) : graphData && graphData.nodes.length ? (
              <GraphViewer
                graphData={graphData}
                width={dims.w}
                height={dims.h}
                view3D={view3D}
                selectedNode={graphSelected}
                onNodeClick={(n) => setGraphSelected(n?.id ?? null)}
              />
            ) : (
              <div className="flex h-full items-center justify-center text-center text-sm text-zinc-400">
                Nessun grafo per questi risultati (il knowledge graph potrebbe essere disattivo).
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
