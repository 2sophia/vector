"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Plug, Plus, RefreshCw, Trash2, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogHeader, DialogTitle, DialogBody, DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { AutoSyncPanel } from "@/components/auto-sync-panel";

type ProviderField = {
  name: string;
  label: string;
  type: string;
  placeholder: string;
  required: boolean;
  secret: boolean;
};
type Provider = { type: string; label: string; enabled: boolean; config_fields: ProviderField[] };
type Source = {
  id: string;
  name: string;
  type: string;
  status: string;
  config: Record<string, unknown>;
  secret_set: boolean;
};

export default function SourcesPage() {
  const [sources, setSources] = useState<Source[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState("sharepoint");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const src = await api.get<{ data: Source[] }>("/sources");
      setSources(src.data || []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    (async () => {
      try {
        const res = await api.get<{ data: Provider[] }>("/sources/types");
        setProviders(res.data || []);
      } catch {
        /* types opzionali */
      }
    })();
  }, [refresh]);

  const providerByType = useMemo(
    () => Object.fromEntries(providers.map((p) => [p.type, p])),
    [providers],
  );
  const currentProvider = providerByType[type];

  function openDialog() {
    const firstEnabled = providers.find((p) => p.enabled);
    setName("");
    setType(firstEnabled?.type || "sharepoint");
    setConfig({});
    setError(null);
    setDialogOpen(true);
  }

  const missingRequired =
    !!currentProvider &&
    currentProvider.config_fields.some((f) => f.required && !(config[f.name] || "").trim());

  async function handleCreateSource() {
    if (!name.trim() || !currentProvider?.enabled || missingRequired) return;
    setSaving(true);
    setError(null);
    try {
      await api.post("/sources", { name: name.trim(), type, config });
      setDialogOpen(false);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleDeleteSource(s: Source) {
    if (!confirm(`Eliminare la source "${s.name}"?`)) return;
    try {
      await api.delete(`/sources/${s.id}`);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="px-8 py-10">
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <div className="flex items-end justify-between gap-4">
          <div className="flex flex-col gap-1.5">
            <h1 className="text-2xl font-semibold tracking-tight">Sources</h1>
            <p className="text-sm text-zinc-500 dark:text-zinc-400">
              Connessioni esterne con credenziali proprie. I secret sono cifrati e mai mostrati.
              Le sync si avviano dalle directory.
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
              <RefreshCw className={cn("size-4", loading && "animate-spin")} />
              Refresh
            </Button>
            <Button size="sm" onClick={openDialog}>
              <Plus className="size-4" />
              Nuova source
            </Button>
          </div>
        </div>

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
            {error}
          </div>
        )}

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Connessioni</CardTitle>
            <CardDescription>
              {sources.length === 0 ? "Nessuna source." : `${sources.length} source`}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {sources.length === 0 ? (
              <div className="flex h-24 items-center justify-center rounded-md border border-dashed border-zinc-300 text-sm text-zinc-400 dark:border-zinc-800 dark:text-zinc-600">
                Aggiungi la prima connessione.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="border-b border-zinc-200 text-left text-xs uppercase text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
                    <tr>
                      <th className="px-2 py-2 font-medium">Nome</th>
                      <th className="px-2 py-2 font-medium">Tipo</th>
                      <th className="px-2 py-2 font-medium">Secret</th>
                      <th className="px-2 py-2"></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
                    {sources.map((s) => (
                      <tr key={s.id}>
                        <td className="px-2 py-2.5 font-medium">
                          <span className="inline-flex items-center gap-2">
                            <Plug className="size-4 text-zinc-400" />
                            {s.name}
                          </span>
                        </td>
                        <td className="px-2 py-2.5 text-zinc-600 dark:text-zinc-400">
                          {providerByType[s.type]?.label || s.type}
                        </td>
                        <td className="px-2 py-2.5">
                          <span
                            className={cn(
                              "rounded-full px-2 py-0.5 text-[11px] font-medium",
                              s.secret_set
                                ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"
                                : "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
                            )}
                          >
                            {s.secret_set ? "configurato" : "mancante"}
                          </span>
                        </td>
                        <td className="px-2 py-2.5 text-right">
                          <Button variant="ghost" size="icon" onClick={() => handleDeleteSource(s)} aria-label="Elimina">
                            <Trash2 className="size-4 text-zinc-500 hover:text-red-600" />
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Scheduling automatico delle sync (cron interno, per tipo source) */}
        <AutoSyncPanel type="sharepoint" label="SharePoint" />
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogHeader>
          <DialogTitle>Nuova source</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="src-name">Nome</Label>
              <Input
                id="src-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="es. Documenti Compliance"
                autoComplete="off"
                autoFocus
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="src-type">Tipo</Label>
              <Select
                id="src-type"
                value={type}
                onChange={(e) => {
                  setType(e.target.value);
                  setConfig({});
                }}
              >
                {providers.map((p) => (
                  <option key={p.type} value={p.type} disabled={!p.enabled}>
                    {p.label}
                    {!p.enabled ? " (presto)" : ""}
                  </option>
                ))}
              </Select>
            </div>

            {/* Campi dinamici del provider scelto */}
            {currentProvider?.config_fields.map((f) => (
              <div key={f.name} className="flex flex-col gap-2">
                <Label htmlFor={`cfg-${f.name}`}>
                  {f.label}
                  {!f.required && <span className="text-zinc-400"> (opzionale)</span>}
                </Label>
                <Input
                  id={`cfg-${f.name}`}
                  type={f.type === "password" ? "password" : "text"}
                  value={config[f.name] || ""}
                  placeholder={f.placeholder}
                  autoComplete={f.type === "password" ? "new-password" : "off"}
                  onChange={(e) => setConfig((c) => ({ ...c, [f.name]: e.target.value }))}
                />
              </div>
            ))}
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => setDialogOpen(false)}>
            Annulla
          </Button>
          <Button
            onClick={handleCreateSource}
            disabled={saving || !name.trim() || !currentProvider?.enabled || missingRequired}
          >
            {saving && <Loader2 className="size-4 animate-spin" />}
            Salva
          </Button>
        </DialogFooter>
      </Dialog>
    </div>
  );
}
