"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, Wand2, Eye, Play, Loader2, Trash2, FileText, Layers, RotateCcw } from "lucide-react";
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
  marked?: number;
  reset?: boolean;
};
type OutlierSample = {
  point_id: string;
  filename?: string | null;
  heading?: string | null;
  similarity: number;
};
type OutlierStats = {
  points_scanned?: number;
  outliers?: number;
  outlier_pct?: number;
  sim_threshold?: number;
  mean_sim?: number;
  samples?: OutlierSample[];
};
type ConflictSample = {
  head: string;
  head_type?: string;
  relation?: string;
  values?: string[];
  value_count?: number;
};
type ConflictStats = {
  graph_enabled?: boolean;
  conflicts?: number;
  samples?: ConflictSample[];
};
type OptimizeResult = {
  dry_run: boolean;
  graph: GraphStats;
  curation: CurationStats;
  redundancy?: RedundancyStats | null;
  outliers?: OutlierStats | null;
  conflicts?: ConflictStats | null;
};
// L'optimize gira come job asincrono: POST ritorna {job_id, running}, poi si fa
// polling sul GET finché completed/failed (il calcolo può durare minuti → no timeout).
type OptimizeJobResponse = {
  job_id?: string;
  status: "idle" | "running" | "completed" | "failed";
  result?: OptimizeResult | null;
  error?: string | null;
  params?: { dry_run?: boolean } | null;
  already_running?: boolean;
};

export default function OptimizePage() {
  const params = useParams<{ id: string }>();
  const vsid = params.id;

  const [minScore, setMinScore] = useState(0.6);
  const [minLen, setMinLen] = useState(3);
  const [dropNumeric, setDropNumeric] = useState(true);
  const [denseThreshold, setDenseThreshold] = useState(0.96);
  const [markRedundant, setMarkRedundant] = useState(false);
  const [includeOutliers, setIncludeOutliers] = useState(false);
  const [includeConflicts, setIncludeConflicts] = useState(false);

  const [busy, setBusy] = useState<false | "preview" | "apply" | "reset">(false);
  const [result, setResult] = useState<OptimizeResult | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Polling di un job fino a completed/failed. Riusato sia dal lancio (run) sia dal
  // recupero dopo un F5. Cap a 20 min: oltre, si molla il polling (il job continua
  // lato server e si ritrova al refresh successivo).
  const pollUntilDone = useCallback(
    async (jobId: string): Promise<OptimizeJobResponse> => {
      const startedAt = performance.now();
      const TIMEOUT_MS = 20 * 60 * 1000;
      let job = await api.get<OptimizeJobResponse>(`/vector_stores/${vsid}/optimize/${jobId}`);
      while (job.status === "running") {
        if (performance.now() - startedAt > TIMEOUT_MS)
          throw new Error("timeout polling: il job è ancora in corso, riprova tra poco");
        await new Promise((res) => setTimeout(res, 2000));
        job = await api.get<OptimizeJobResponse>(`/vector_stores/${vsid}/optimize/${jobId}`);
      }
      return job;
    },
    [vsid],
  );

  const run = useCallback(
    async (dryRun: boolean, reset = false) => {
      setBusy(reset ? "reset" : dryRun ? "preview" : "apply");
      setError(null);
      const applyRed = markRedundant && !dryRun && !reset;
      const qs =
        `min_score=${minScore}&min_entity_len=${minLen}` +
        `&drop_numeric=${dropNumeric}&dense_threshold=${denseThreshold}` +
        `&apply_redundancy=${applyRed}&reset_redundancy=${reset}&dry_run=${dryRun}` +
        `&include_outliers=${includeOutliers}&include_conflicts=${includeConflicts}`;
      const flags =
        `--min-score ${minScore} --min-entity-len ${minLen}` +
        `${dropNumeric ? " --drop-numeric" : ""} --sim ${denseThreshold}` +
        `${applyRed ? " --mark-redundant" : ""}${reset ? " --reset-redundant" : ""}` +
        `${dryRun ? " --dry-run" : ""}`;
      try {
        // 1) avvia il job. Idempotente: se ce n'è già uno in corso per lo store, il
        // backend ritorna quello (anti-flood) e noi ci riagganciamo col polling.
        const started = await api.post<OptimizeJobResponse>(
          `/vector_stores/${vsid}/optimize?${qs}`,
        );
        if (started.already_running)
          setLog((prev) => [...prev, "↻ un'ottimizzazione è già in corso: mi riaggancio a quella."]);
        if (!started.job_id) throw new Error("nessun job avviato");
        // 2) polling finché è pronto (può durare minuti)
        const job = await pollUntilDone(started.job_id);
        if (job.status === "failed" || !job.result)
          throw new Error(job.error || "ottimizzazione fallita");
        const r = job.result;
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
          if (rd.marked)
            lines.push(`✓ marcati ${rd.marked} ridondanti (soppressi a search-time)`);
          if (rd.reset) lines.push("✓ marcatura ridondanti azzerata");
        }
        const ol = r.outliers;
        if (ol && ol.points_scanned) {
          lines.push(
            `→ outlier semantici (sim < ${ol.sim_threshold}): ${ol.outliers}/${ol.points_scanned} ` +
              `(${ol.outlier_pct}%, sim media ${ol.mean_sim}) — diagnostica, nulla rimosso`,
          );
        }
        const cf = r.conflicts;
        if (cf && cf.graph_enabled) {
          lines.push(
            `→ conflitti relazione (valori multipli): ${cf.conflicts} candidati da rivedere`,
          );
        }
        setLog((prev) => [...prev, ...lines, ""]);
      } catch (e) {
        setError(String(e));
        setLog((prev) => [...prev, `✗ errore: ${String(e)}`, ""]);
      } finally {
        setBusy(false);
      }
    },
    [vsid, minScore, minLen, dropNumeric, denseThreshold, markRedundant, includeOutliers, includeConflicts, pollUntilDone],
  );

  // Recupero dopo un F5: se per questo store c'è già un job in corso, ci si riaggancia
  // (mostra "in corso" + polling) invece di lasciar rilanciare un doppione. Il backend
  // è comunque idempotente, questo è il lato UI che ritrova lo stato perso al refresh.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const active = await api.get<OptimizeJobResponse>(`/vector_stores/${vsid}/optimize`);
        if (cancelled || active.status !== "running" || !active.job_id) return;
        setBusy(active.params?.dry_run ? "preview" : "apply");
        setLog((prev) => [...prev, `↻ ripreso un'ottimizzazione già in corso (${active.job_id})…`]);
        const job = await pollUntilDone(active.job_id);
        if (cancelled) return;
        if (job.status === "failed" || !job.result) setError(job.error || "ottimizzazione fallita");
        else {
          setResult(job.result);
          setLog((prev) => [...prev, "✓ ottimizzazione completata.", ""]);
        }
      } catch {
        /* nessun job attivo o backend non ancora aggiornato: si ignora */
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [vsid, pollUntilDone]);

  const g = result?.graph;
  const cur = result?.curation;
  const ol = result?.outliers;
  const cf = result?.conflicts;

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

            {/* marca ridondanti all'applica */}
            <div className="flex items-center justify-between">
              <div className="flex flex-col gap-1">
                <Label>Marca i near-duplicate all&apos;applica</Label>
                <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                  Off = solo conteggio. On = i ridondanti vengono soppressi a search-time
                  (resta il rappresentante). Reversibile col reset.
                </p>
              </div>
              <Button
                variant={markRedundant ? "default" : "outline"}
                size="sm"
                onClick={() => setMarkRedundant((v) => !v)}
              >
                {markRedundant ? "Attivo" : "Disattivo"}
              </Button>
            </div>

            {/* diagnostica outlier semantici (sola lettura) */}
            <div className="flex items-center justify-between">
              <div className="flex flex-col gap-1">
                <Label>Outlier semantici (diagnostica)</Label>
                <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                  Segnala i chunk lontani dal centroide del corpus (candidati off-topic).
                  Sola lettura: non rimuove nulla, l&apos;outlier raro-ma-prezioso lo decidi tu.
                </p>
              </div>
              <Button
                variant={includeOutliers ? "default" : "outline"}
                size="sm"
                onClick={() => setIncludeOutliers((v) => !v)}
              >
                {includeOutliers ? "Attivo" : "Disattivo"}
              </Button>
            </div>

            {/* diagnostica conflitti relazione (sola lettura) */}
            <div className="flex items-center justify-between">
              <div className="flex flex-col gap-1">
                <Label>Conflitti relazione (diagnostica)</Label>
                <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                  Coppie (entità, relazione) con valori multipli sul grafo: candidati
                  conflitto da rivedere. NON è una contraddizione confermata (i valori
                  multipli sono spesso legittimi) — solo un segnale per l&apos;umano.
                </p>
              </div>
              <Button
                variant={includeConflicts ? "default" : "outline"}
                size="sm"
                onClick={() => setIncludeConflicts((v) => !v)}
              >
                {includeConflicts ? "Attivo" : "Disattivo"}
              </Button>
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
          <Button
            variant="ghost"
            onClick={() => run(false, true)}
            disabled={!!busy}
            className="ml-auto"
            title="Rimuove la marcatura dei ridondanti (i chunk tornano nei risultati)"
          >
            {busy === "reset" ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <RotateCcw className="size-4" />
            )}
            Reset marcatura
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

        {/* OUTLIER SEMANTICI (diagnostica) */}
        {ol && ol.points_scanned ? (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Outlier semantici</CardTitle>
              <CardDescription>
                Chunk lontani dal centroide del corpus — candidati off-topic. Sola
                lettura: per rimuoverli usa l&apos;esclusione file, non si cancella nulla qui.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Stat label="Punti analizzati" value={ol.points_scanned} />
                <Stat label="Outlier" value={ol.outliers} tone="amber" />
                <Stat label="% outlier" value={`${ol.outlier_pct ?? 0}%`} tone="amber" />
                <Stat label="Sim media" value={ol.mean_sim} />
              </div>
              {ol.samples && ol.samples.length > 0 && (
                <div className="overflow-hidden rounded-lg border border-zinc-200 dark:border-zinc-800">
                  <table className="w-full text-xs">
                    <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900/50 dark:text-zinc-400">
                      <tr>
                        <th className="px-3 py-2 font-medium">File</th>
                        <th className="px-3 py-2 font-medium">Sezione</th>
                        <th className="px-3 py-2 text-right font-medium">Similarità</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
                      {ol.samples.map((s) => (
                        <tr key={s.point_id}>
                          <td className="px-3 py-2 font-medium text-zinc-800 dark:text-zinc-200">
                            {s.filename || "—"}
                          </td>
                          <td className="px-3 py-2 text-zinc-500 dark:text-zinc-400">
                            {s.heading || "—"}
                          </td>
                          <td className="px-3 py-2 text-right font-mono tabular-nums text-amber-600 dark:text-amber-400">
                            {s.similarity.toFixed(3)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        ) : null}

        {/* CONFLITTI RELAZIONE (diagnostica) */}
        {cf && cf.graph_enabled ? (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                Conflitti relazione
                <span className="ml-2 text-sm font-normal text-amber-600 dark:text-amber-400">
                  {cf.conflicts ?? 0} candidati
                </span>
              </CardTitle>
              <CardDescription>
                Coppie (entità, relazione) con valori multipli sul grafo. Da rivedere a
                mano: un valore multiplo è spesso legittimo, NON è una contraddizione
                confermata. Nessun modello, nessuna cancellazione.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {cf.samples && cf.samples.length > 0 ? (
                <div className="flex flex-col gap-2">
                  {cf.samples.map((c, i) => (
                    <div
                      key={i}
                      className="rounded-lg border border-zinc-200 bg-white p-3 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                    >
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-semibold text-zinc-800 dark:text-zinc-100">
                          {c.head}
                        </span>
                        <span className="rounded bg-indigo-50 px-1.5 py-0.5 font-mono text-[10px] text-indigo-600 dark:bg-indigo-950/40 dark:text-indigo-400">
                          {c.relation}
                        </span>
                        <span className="text-zinc-400">→ {c.value_count} valori:</span>
                      </div>
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {(c.values || []).map((v, j) => (
                          <span
                            key={j}
                            className="rounded bg-amber-50 px-1.5 py-0.5 text-amber-700 dark:bg-amber-950/30 dark:text-amber-400"
                          >
                            {v}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-zinc-500 dark:text-zinc-400">
                  Nessun candidato conflitto rilevato.
                </p>
              )}
            </CardContent>
          </Card>
        ) : null}
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
