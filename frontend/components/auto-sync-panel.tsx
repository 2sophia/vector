"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Clock,
  Loader2,
  RefreshCw,
  CheckCircle2,
  AlertTriangle,
  CalendarClock,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

type Schedule = {
  type: string;
  enabled: boolean;
  cron: string | null;
  next_run: number | null;
  last_run: number | null;
};
type Run = {
  type: string;
  started_at: number;
  finished_at: number | null;
  status: string;
  detail?: { total?: number } | null;
  error?: string | null;
};

// preset → espressione cron
const PRESETS: { label: string; cron: string }[] = [
  { label: "Ogni 5 minuti", cron: "*/5 * * * *" },
  { label: "Ogni 15 minuti", cron: "*/15 * * * *" },
  { label: "Ogni 30 minuti", cron: "*/30 * * * *" },
  { label: "Ogni ora", cron: "0 * * * *" },
  { label: "Ogni giorno alle 03:00", cron: "0 3 * * *" },
  { label: "Ogni giorno alle 06:00", cron: "0 6 * * *" },
];

function fmtTs(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("it-IT", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

export function AutoSyncPanel({ type = "sharepoint", label = "SharePoint" }: { type?: string; label?: string }) {
  const [enabled, setEnabled] = useState(false);
  const [cron, setCron] = useState("0 3 * * *");
  const [isCustom, setIsCustom] = useState(false);
  const [sched, setSched] = useState<Schedule | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const s = await api.get<Schedule>(`/sync/schedule/${type}`);
      setSched(s);
      setEnabled(s.enabled);
      if (s.cron) {
        setCron(s.cron);
        setIsCustom(!PRESETS.some((p) => p.cron === s.cron));
      }
      const r = await api.get<{ data: Run[] }>(`/sync/runs/${type}`);
      setRuns(r.data || []);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [type]);

  useEffect(() => {
    load();
  }, [load]);

  async function save() {
    setSaving(true);
    setErr(null);
    try {
      const s = await api.put<Schedule>(`/sync/schedule/${type}`, { enabled, cron });
      setSched(s);
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <CalendarClock className="size-4 text-indigo-500" />
          Sincronizzazione automatica · {label}
        </CardTitle>
        <CardDescription>
          Pianifica un re-sync ricorrente di tutte le sync {label}. Lo scheduler interno
          rispetta l&apos;overlap (se una sync è già in corso, salta).
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {err && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
            {err}
          </div>
        )}

        <div className="flex flex-wrap items-end gap-4">
          {/* toggle attivo */}
          <button
            type="button"
            onClick={() => setEnabled((v) => !v)}
            className={cn(
              "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors",
              enabled
                ? "border-emerald-300 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300"
                : "border-zinc-200 text-zinc-500 dark:border-zinc-800 dark:text-zinc-400",
            )}
          >
            <span className={cn("flex h-4 w-7 items-center rounded-full px-0.5 transition-colors", enabled ? "bg-emerald-500" : "bg-zinc-300 dark:bg-zinc-700")}>
              <span className={cn("size-3 rounded-full bg-white transition-transform", enabled && "translate-x-3")} />
            </span>
            {enabled ? "Attiva" : "Disattivata"}
          </button>

          {/* preset */}
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="cron-preset" className="text-xs">Frequenza</Label>
            <Select
              id="cron-preset"
              value={isCustom ? "__custom" : cron}
              onChange={(e) => {
                if (e.target.value === "__custom") {
                  setIsCustom(true);
                } else {
                  setIsCustom(false);
                  setCron(e.target.value);
                }
              }}
            >
              {PRESETS.map((p) => (
                <option key={p.cron} value={p.cron}>{p.label}</option>
              ))}
              <option value="__custom">Personalizzato (cron)…</option>
            </Select>
          </div>

          {/* cron avanzato */}
          {isCustom && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="cron-custom" className="text-xs">Espressione cron</Label>
              <Input
                id="cron-custom"
                value={cron}
                onChange={(e) => setCron(e.target.value)}
                placeholder="*/5 * * * *"
                className="font-mono"
              />
            </div>
          )}

          <Button size="sm" onClick={save} disabled={saving || loading}>
            {saving ? <Loader2 className="size-4 animate-spin" /> : <Clock className="size-4" />}
            Salva schedule
          </Button>
          <Button variant="ghost" size="icon" onClick={load} disabled={loading} aria-label="Aggiorna">
            <RefreshCw className={cn("size-4", loading && "animate-spin")} />
          </Button>
        </div>

        {/* stato */}
        <div className="flex flex-wrap gap-4 text-xs text-zinc-500 dark:text-zinc-400">
          <span>cron attivo: <span className="font-mono text-zinc-700 dark:text-zinc-300">{sched?.cron || "—"}</span></span>
          <span>prossima: <span className="font-medium text-zinc-700 dark:text-zinc-300">{sched?.enabled ? fmtTs(sched?.next_run) : "—"}</span></span>
          <span>ultima: {fmtTs(sched?.last_run)}</span>
        </div>

        {/* ultimi run */}
        <div className="flex flex-col gap-1.5">
          <Label className="text-xs">Ultimi run</Label>
          {runs.length === 0 ? (
            <p className="text-xs text-zinc-400 dark:text-zinc-600">Nessun run ancora.</p>
          ) : (
            <div className="flex flex-col gap-1">
              {runs.map((r, i) => {
                const ok = r.status === "OK";
                return (
                  <div
                    key={i}
                    className="flex items-center justify-between gap-2 rounded-md border border-zinc-200 px-2.5 py-1.5 text-xs dark:border-zinc-800"
                  >
                    <span className="inline-flex items-center gap-2">
                      {ok ? (
                        <CheckCircle2 className="size-3.5 text-emerald-500" />
                      ) : (
                        <AlertTriangle className="size-3.5 text-red-500" />
                      )}
                      <span className="font-medium">{fmtTs(r.started_at)}</span>
                      <span className="text-zinc-500 dark:text-zinc-400">
                        {ok
                          ? `${r.detail?.total ?? 0} job sincronizzati`
                          : (r.error || "errore").slice(0, 80)}
                      </span>
                    </span>
                    <span
                      className={cn(
                        "rounded-full px-2 py-0.5 text-[10px] font-medium",
                        ok
                          ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300"
                          : "bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300",
                      )}
                    >
                      {r.status}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
