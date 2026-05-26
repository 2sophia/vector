"use client";

import { useCallback, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, Wand2, Eye, Play, Loader2, Trash2, FileText, Layers } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

type GraphStats = {
  enabled: boolean;
  min_score?: number;
  min_entity_len?: number;
  drop_numeric?: boolean;
  dry_run?: boolean;
  entities_before?: number;
  mentions_before?: number;
  weak_mentions?: number;
  junk_entities?: number;
  entities_after?: number;
  mentions_after?: number;
  entities_removed?: number;
  mentions_removed?: number;
};
type CurationStats = {
  total_documents?: number;
  distinct_contents?: number;
  boilerplate_contents?: number;
  max_doc_frequency?: number;
};
type RedundancyStats = {
  points?: number;
  clusters?: number;
  redundant?: number;
  kept?: number;
  reduction_pct?: number;
  variants_preserved?: number;
  dense_threshold?: number;
};
type OptimizeResult = {
  dry_run: boolean;
  graph: GraphStats;
  curation: CurationStats;
  redundancy?: RedundancyStats | null;
};

export default function OptimizePage() {
  const params = useParams<{ id: string }>();
  const vsid = params.id;

  const [minScore, setMinScore] = useState(0.6);
  const [minLen, setMinLen] = useState(3);
  const [dropNumeric, setDropNumeric] = useState(true);
  const [denseThreshold, setDenseThreshold] = useState(0.96);

  const [busy, setBusy] = useState<false | "preview" | "apply">(false);
  const [result, setResult] = useState<OptimizeResult | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (dryRun: boolean) => {
      setBusy(dryRun ? "preview" : "apply");
      setError(null);
      const qs =
        `min_score=${minScore}&min_entity_len=${minLen}` +
        `&drop_numeric=${dropNumeric}&dense_threshold=${denseThreshold}&dry_run=${dryRun}`;
      const flags =
        `--min-score ${minScore} --min-entity-len ${minLen}` +
        `${dropNumeric ? " --drop-numeric" : ""} --sim ${denseThreshold}` +
        `${dryRun ? " --dry-run" : ""}`;
      try {
        const r = await api.post<OptimizeResult>(`/vector_stores/${vsid}/optimize?${qs}`);
        setResult(r);
        const g = r.graph;
        const lines: string[] = [`$ optimize ${flags}`];
        if (!g.enabled) {
          lines.push("grafo disabilitato — niente da fare");
        } else {
          lines.push(`→ stato: ${g.entities_before} entità · ${g.mentions_before} menzioni`);
          lines.push(`→ menzioni deboli (score < ${minScore}): ${g.weak_mentions}`);
          lines.push(`→ entità spazzatura (corte/numeriche): ${g.junk_entities}`);
          if (dryRun) {
            lines.push("# dry-run: nulla è stato cancellato. Premi «Applica» per eseguire.");
          } else {
            lines.push(
              `✓ entità ${g.entities_before} → ${g.entities_after} (−${g.entities_removed})`,
            );
            lines.push(
              `✓ menzioni ${g.mentions_before} → ${g.mentions_after} (−${g.mentions_removed})`,
            );
            lines.push("done.");
          }
        }
        const rd = r.redundancy;
        if (rd && rd.points) {
          lines.push(
            `→ ridondanza (sim ≥ ${denseThreshold}): ${rd.clusters} cluster · ` +
              `${rd.redundant} near-duplicate (−${rd.reduction_pct}%)`,
          );
          if (rd.variants_preserved)
            lines.push(`→ varianti preservate (solo-dense): ${rd.variants_preserved}`);
        }
        setLog((prev) => [...prev, ...lines, ""]);
      } catch (e) {
        setError(String(e));
        setLog((prev) => [...prev, `✗ errore: ${String(e)}`, ""]);
      } finally {
        setBusy(false);
      }
    },
    [vsid, minScore, minLen, dropNumeric, denseThreshold],
  );

  const g = result?.graph;
  const cur = result?.curation;

  return (
    <div className="px-8 py-10">
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <Link
          href={`/stores/${vsid}`}
          className="inline-flex items-center gap-1.5 text-sm text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
        >
          <ArrowLeft className="size-4" />
          Vector store
        </Link>

        <div className="flex flex-col gap-1.5">
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Wand2 className="size-6 text-indigo-500" />
            Ottimizzazione
          </h1>
          <p className="text-sm text-zinc-500 dark:text-zinc-400">
            Ripulisce il knowledge graph <strong>senza re-ingest</strong>: rimuove le
            menzioni a bassa confidenza e le entità spazzatura. Idempotente — prova
            l&apos;anteprima, poi applica.
          </p>
        </div>

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
            {error}
          </div>
        )}

        {/* CONFIG */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Parametri</CardTitle>
            <CardDescription>Filtri agnostici: valgono per qualsiasi dominio.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-6">
            {/* min_score slider */}
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="min-score">Soglia confidenza menzioni</Label>
                <span className="font-mono text-sm text-indigo-600 dark:text-indigo-400">
                  {minScore.toFixed(2)}
                </span>
              </div>
              <input
                id="min-score"
                type="range"
                min={0.5}
                max={0.9}
                step={0.05}
                value={minScore}
                onChange={(e) => setMinScore(parseFloat(e.target.value))}
                className="w-full accent-indigo-500"
              />
              <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                Rimuove le menzioni entità con score inferiore. Più alto = più aggressivo.
              </p>
            </div>

            {/* min_entity_len */}
            <div className="flex flex-col gap-2">
              <Label htmlFor="min-len">Lunghezza minima nome entità</Label>
              <Input
                id="min-len"
                type="number"
                min={1}
                max={10}
                value={minLen}
                onChange={(e) => setMinLen(Math.max(1, parseInt(e.target.value || "1", 10)))}
                className="max-w-[120px]"
              />
              <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                Scarta i frammenti troppo corti (es. «TE», «N»).
              </p>
            </div>

            {/* drop_numeric toggle */}
            <div className="flex items-center justify-between">
              <div className="flex flex-col gap-1">
                <Label>Rimuovi numerazioni</Label>
                <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                  Scarta entità fatte solo di cifre/punteggiatura (es. «1.6.1.12.3»).
                </p>
              </div>
              <Button
                variant={dropNumeric ? "default" : "outline"}
                size="sm"
                onClick={() => setDropNumeric((v) => !v)}
              >
                {dropNumeric ? "Attivo" : "Disattivo"}
              </Button>
            </div>

            {/* similarità ridondanza (dense ∩ sparse) */}
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="dense-thr">Similarità ridondanza (dense ∩ sparse)</Label>
                <span className="font-mono text-sm text-indigo-600 dark:text-indigo-400">
                  {denseThreshold.toFixed(2)}
                </span>
              </div>
              <input
                id="dense-thr"
                type="range"
                min={0.9}
                max={0.99}
                step={0.01}
                value={denseThreshold}
                onChange={(e) => setDenseThreshold(parseFloat(e.target.value))}
                className="w-full accent-indigo-500"
              />
              <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                Chunk near-duplicate: simili sopra soglia <em>e</em> concordi nello sparse
                (il solo-dense è una variante e viene preservato). Più alto = solo i
                quasi-identici.
              </p>
            </div>
          </CardContent>
        </Card>

        {/* AZIONI */}
        <div className="flex items-center gap-2">
          <Button variant="outline" onClick={() => run(true)} disabled={!!busy}>
            {busy === "preview" ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Eye className="size-4" />
            )}
            Anteprima (dry-run)
          </Button>
          <Button onClick={() => run(false)} disabled={!!busy}>
            {busy === "apply" ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Play className="size-4" />
            )}
            Applica ottimizzazione
          </Button>
        </div>

        {/* loader bar */}
        {busy && (
          <div className="h-1 w-full overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
            <div className="h-full w-1/2 animate-pulse rounded-full bg-indigo-500" />
          </div>
        )}

        {/* PREVIEW NUMBERS */}
        {g && g.enabled && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Menzioni deboli" value={g.weak_mentions} icon={<Trash2 className="size-4" />} tone="amber" />
            <Stat label="Entità spazzatura" value={g.junk_entities} icon={<Trash2 className="size-4" />} tone="amber" />
            <Stat label="Entità (totali)" value={result?.dry_run ? g.entities_before : g.entities_after} icon={<FileText className="size-4" />} />
            <Stat label="Menzioni (totali)" value={result?.dry_run ? g.mentions_before : g.mentions_after} icon={<FileText className="size-4" />} />
          </div>
        )}

        {/* REDUNDANCY (dense ∩ sparse) */}
        {result?.redundancy && result.redundancy.points ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Cluster ridondanti" value={result.redundancy.clusters} icon={<Layers className="size-4" />} />
            <Stat label="Chunk near-duplicate" value={result.redundancy.redundant} icon={<Trash2 className="size-4" />} tone="amber" />
            <Stat label="Riduzione possibile" value={`${result.redundancy.reduction_pct ?? 0}%`} tone="amber" />
            <Stat label="Varianti preservate" value={result.redundancy.variants_preserved} icon={<FileText className="size-4" />} />
          </div>
        ) : null}

        {/* LOG */}
        {log.length > 0 && (
          <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-4 font-mono text-xs leading-relaxed text-zinc-300">
            {log.map((l, i) => (
              <div
                key={i}
                className={cn(
                  l.startsWith("$") && "text-zinc-500",
                  l.startsWith("✓") && "text-green-400",
                  l.startsWith("✗") && "text-red-400",
                  l.startsWith("#") && "text-amber-400",
                  l.startsWith("→") && "text-zinc-400",
                )}
              >
                {l || " "}
              </div>
            ))}
          </div>
        )}

        {/* CURATION (diagnostica) */}
        {cur && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Data curation</CardTitle>
              <CardDescription>
                Già coerente per costruzione (si aggiorna a ogni ingest). Qui solo diagnostica.
              </CardDescription>
            </CardHeader>
            <CardContent className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Stat label="Documenti" value={cur.total_documents} />
              <Stat label="Contenuti distinti" value={cur.distinct_contents} />
              <Stat label="Boilerplate" value={cur.boilerplate_contents} tone="amber" />
              <Stat label="Max frequenza" value={cur.max_doc_frequency} />
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  icon,
  tone,
}: {
  label: string;
  value?: number | string;
  icon?: React.ReactNode;
  tone?: "amber";
}) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900">
      <span className="inline-flex items-center gap-1.5 text-[11px] text-zinc-500 dark:text-zinc-400">
        {icon}
        {label}
      </span>
      <span
        className={cn(
          "text-xl font-semibold tabular-nums",
          tone === "amber" && "text-amber-600 dark:text-amber-400",
        )}
      >
        {value ?? "—"}
      </span>
    </div>
  );
}
