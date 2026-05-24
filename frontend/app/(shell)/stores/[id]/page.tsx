"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  ArrowRight,
  Database,
  FolderTree,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogHeader, DialogTitle, DialogBody, DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { RESERVED_PROP_KEYS, softSlug, softPropKey } from "@/lib/validation";

type Directory = {
  id: string;
  name: string;
  slug: string;
  properties: Record<string, unknown>;
  vector_store_id: string;
  file_count: number;
  created_at: number;
};
type Prop = { key: string; value: string };

export default function VectorStoreDetailPage() {
  const params = useParams<{ id: string }>();
  const vectorStoreId = params.id;

  const [storeName, setStoreName] = useState<string>("");
  const [dirs, setDirs] = useState<Directory[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // form
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [props, setProps] = useState<Prop[]>([]);

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

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get<{ data: Directory[] }>(
        `/directories?vector_store_id=${vectorStoreId}`,
      );
      setDirs(res.data || []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [vectorStoreId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  function openDialog() {
    setName("");
    setSlug("");
    setSlugTouched(false);
    setProps([]);
    setError(null);
    setDialogOpen(true);
  }

  // chiavi property normalizzate + flag se qualcuna è riservata
  const reservedHit = props.some((p) => p.key && RESERVED_PROP_KEYS.has(p.key));

  async function handleCreate() {
    if (!name.trim() || !slug || reservedHit) return;
    setCreating(true);
    setError(null);
    const properties: Record<string, string> = {};
    for (const { key, value } of props) {
      if (key && !RESERVED_PROP_KEYS.has(key)) properties[key] = value;
    }
    try {
      await api.post("/directories", {
        name: name.trim(),
        slug,
        properties,
        vector_store_id: vectorStoreId,
      });
      setDialogOpen(false);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(d: Directory) {
    if (
      !confirm(
        `Eliminare la directory "${d.name}"?\nVengono rimossi TUTTI i suoi file (${d.file_count}) e i relativi chunk.`,
      )
    )
      return;
    try {
      await api.delete(`/directories/${d.id}`);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="px-8 py-10">
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <Link
          href="/stores"
          className="inline-flex items-center gap-1.5 text-sm text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
        >
          <ArrowLeft className="size-4" />
          Vector Stores
        </Link>

        <div className="flex items-end justify-between gap-4">
          <div className="flex flex-col gap-1.5">
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <Database className="size-6 text-indigo-500" />
              {storeName || "…"}
            </h1>
            <p className="text-sm text-zinc-500 dark:text-zinc-400">
              Le directory di questo vector store. I file caricati in una directory
              ne ereditano slug e proprietà.
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
              <RefreshCw className={cn("size-4", loading && "animate-spin")} />
              Refresh
            </Button>
            <Button size="sm" onClick={openDialog}>
              <Plus className="size-4" />
              Nuova directory
            </Button>
          </div>
        </div>

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
            {error}
          </div>
        )}

        {dirs.length === 0 ? (
          <Card>
            <CardContent className="py-12">
              <div className="flex flex-col items-center justify-center gap-3 text-center">
                <FolderTree className="size-8 text-zinc-300 dark:text-zinc-700" />
                <p className="text-sm text-zinc-500 dark:text-zinc-400">
                  {loading ? "Caricamento…" : "Nessuna directory — creane una per iniziare."}
                </p>
              </div>
            </CardContent>
          </Card>
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {dirs.map((d) => {
              const propEntries = Object.entries(d.properties || {});
              return (
                <Card key={d.id} className="flex flex-col">
                  <CardHeader className="pb-2">
                    <CardTitle className="flex items-center justify-between gap-2 text-base">
                      <span className="inline-flex items-center gap-2 truncate">
                        <FolderTree className="size-4 text-indigo-500" />
                        {d.name}
                      </span>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => handleDelete(d)}
                        aria-label="Elimina directory"
                      >
                        <Trash2 className="size-4 text-zinc-400 hover:text-red-600" />
                      </Button>
                    </CardTitle>
                    <CardDescription>
                      <span className="font-mono text-[11px]">{d.slug}</span> · {d.file_count} file
                    </CardDescription>
                  </CardHeader>
                  <CardContent className="flex flex-1 flex-col justify-between gap-3">
                    <div className="flex flex-wrap gap-1">
                      {propEntries.length === 0 ? (
                        <span className="text-[11px] text-zinc-400">Nessuna proprietà</span>
                      ) : (
                        propEntries.map(([k, v]) => (
                          <span
                            key={k}
                            className="rounded-full bg-zinc-100 px-2 py-0.5 text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"
                            title={`${k}: ${String(v)}`}
                          >
                            <span className="font-mono">{k}</span>={String(v)}
                          </span>
                        ))
                      )}
                    </div>
                    <Link href={`/directories/${d.id}`}>
                      <Button variant="outline" size="sm" className="w-full justify-between">
                        Apri
                        <ArrowRight className="size-4" />
                      </Button>
                    </Link>
                  </CardContent>
                </Card>
              );
            })}
          </div>
        )}
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogHeader>
          <DialogTitle>Nuova directory</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="dir-name">Nome</Label>
              <Input
                id="dir-name"
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  if (!slugTouched) setSlug(softSlug(e.target.value));
                }}
                placeholder="es. Pubblica"
                autoFocus
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="dir-slug">Slug</Label>
              <Input
                id="dir-slug"
                value={slug}
                onChange={(e) => {
                  setSlug(softSlug(e.target.value));
                  setSlugTouched(true);
                }}
                placeholder="es. pubblica"
                className="font-mono text-sm"
              />
              <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                Identificatore univoco della directory. Solo <code className="font-mono">a-z 0-9 - _</code>;
                spazi e maiuscole vengono convertiti.
              </p>
            </div>

            <div className="flex flex-col gap-2">
              <Label>Proprietà custom (opzionali)</Label>
              {props.map((p, i) => {
                const reserved = !!p.key && RESERVED_PROP_KEYS.has(p.key);
                return (
                  <div key={i} className="flex flex-col gap-1">
                    <div className="flex items-center gap-2">
                      <Input
                        value={p.key}
                        onChange={(e) =>
                          setProps((arr) =>
                            arr.map((x, idx) =>
                              idx === i ? { ...x, key: softPropKey(e.target.value) } : x,
                            ),
                          )
                        }
                        placeholder="chiave"
                        className={cn(
                          "max-w-[180px] font-mono text-xs",
                          reserved && "border-red-400 focus-visible:ring-red-400",
                        )}
                      />
                      <Input
                        value={p.value}
                        onChange={(e) =>
                          setProps((arr) =>
                            arr.map((x, idx) => (idx === i ? { ...x, value: e.target.value } : x)),
                          )
                        }
                        placeholder="valore"
                        className="text-xs"
                      />
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => setProps((arr) => arr.filter((_, idx) => idx !== i))}
                        aria-label="Rimuovi"
                      >
                        <X className="size-4 text-zinc-500 hover:text-red-600" />
                      </Button>
                    </div>
                    {reserved && (
                      <span className="text-[11px] text-red-600 dark:text-red-400">
                        Chiave riservata, scegline un&apos;altra.
                      </span>
                    )}
                  </div>
                );
              })}
              <div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setProps((arr) => [...arr, { key: "", value: "" }])}
                >
                  <Plus className="size-4" />
                  Aggiungi proprietà
                </Button>
              </div>
            </div>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => setDialogOpen(false)}>
            Annulla
          </Button>
          <Button onClick={handleCreate} disabled={creating || !name.trim() || !slug || reservedHit}>
            {creating && <Loader2 className="size-4 animate-spin" />}
            Crea
          </Button>
        </DialogFooter>
      </Dialog>
    </div>
  );
}
