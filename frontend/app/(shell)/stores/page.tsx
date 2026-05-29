"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Database, FolderTree, Plus, RefreshCw, Trash2, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogHeader, DialogTitle, DialogBody, DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

type VectorStore = {
  id: string;
  name: string;
  status: string;
  usage_bytes: number;
  created_at: number;
  file_counts: { total: number; completed: number; in_progress: number; failed: number };
};

type ListResponse = { object: string; data: VectorStore[] };

export default function StoresPage() {
  const [stores, setStores] = useState<VectorStore[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get<ListResponse>("/vector_stores");
      setStores(res.data || []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleCreate() {
    if (!name.trim()) return;
    setCreating(true);
    setError(null);
    try {
      await api.post("/vector_stores", { name: name.trim() });
      setName("");
      setDialogOpen(false);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(s: VectorStore) {
    if (!confirm(`Eliminare il vector store "${s.name}"?\nVengono rimossi anche i punti Qdrant, i file e i job associati.`))
      return;
    try {
      await api.delete(`/vector_stores/${s.id}`);
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
            <h1 className="text-2xl font-semibold tracking-tight">Vector Stores</h1>
            <p className="text-sm text-zinc-500 dark:text-zinc-400">
              Collezioni Qdrant. Ogni store raccoglie i chunk indicizzati dei file che gli colleghi.
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
              <RefreshCw className={cn("size-4", loading && "animate-spin")} />
              Refresh
            </Button>
            <Button size="sm" onClick={() => setDialogOpen(true)}>
              <Plus className="size-4" />
              Nuovo
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
            <CardTitle className="text-base">Store</CardTitle>
            <CardDescription>
              {stores.length === 0 ? "Nessun vector store." : `${stores.length} store`}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {stores.length === 0 ? (
              <div className="flex h-32 items-center justify-center rounded-md border border-dashed border-zinc-300 text-sm text-zinc-400 dark:border-zinc-800 dark:text-zinc-600">
                Crea il primo vector store.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="border-b border-zinc-200 text-left text-xs uppercase text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
                    <tr>
                      <th className="px-2 py-2 font-medium">Nome</th>
                      <th className="px-2 py-2 font-medium">ID</th>
                      <th className="px-2 py-2 text-right font-medium">File</th>
                      <th className="px-2 py-2 font-medium">Creato</th>
                      <th className="px-2 py-2"></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
                    {stores.map((s) => (
                      <tr key={s.id}>
                        <td className="px-2 py-2.5 font-medium">
                          <Link
                            href={`/stores/${s.id}`}
                            className="inline-flex items-center gap-2 transition-colors hover:text-indigo-600"
                          >
                            <Database className="size-4 text-zinc-400" />
                            {s.name}
                          </Link>
                        </td>
                        <td className="px-2 py-2.5 font-mono text-xs text-zinc-500 dark:text-zinc-400">
                          {s.id}
                        </td>
                        <td className="px-2 py-2.5 text-right text-zinc-600 dark:text-zinc-400">
                          {s.file_counts?.total ?? 0}
                          {(s.file_counts?.failed ?? 0) > 0 && (
                            <span className="ml-1.5 rounded-full bg-red-50 px-1.5 py-0.5 text-[11px] font-medium text-red-600 dark:bg-red-950/30 dark:text-red-400">
                              {s.file_counts.failed} falliti
                            </span>
                          )}
                        </td>
                        <td className="px-2 py-2.5 text-xs text-zinc-500 dark:text-zinc-400">
                          {new Date(s.created_at * 1000).toLocaleDateString("it-IT", {
                            day: "2-digit",
                            month: "short",
                            year: "numeric",
                          })}
                        </td>
                        <td className="px-2 py-2.5 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <Link href={`/stores/${s.id}`}>
                              <Button variant="ghost" size="sm">
                                <FolderTree className="size-4" />
                                Apri
                              </Button>
                            </Link>
                            <Button variant="ghost" size="icon" onClick={() => handleDelete(s)} aria-label="Elimina">
                              <Trash2 className="size-4 text-zinc-500 hover:text-red-600" />
                            </Button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogHeader>
          <DialogTitle>Nuovo vector store</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-2">
            <Label htmlFor="vs-name">Nome</Label>
            <Input
              id="vs-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="es. Documenti Compliance"
              onKeyDown={(e) => e.key === "Enter" && handleCreate()}
              autoFocus
            />
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => setDialogOpen(false)}>
            Annulla
          </Button>
          <Button onClick={handleCreate} disabled={creating || !name.trim()}>
            {creating && <Loader2 className="size-4 animate-spin" />}
            Crea
          </Button>
        </DialogFooter>
      </Dialog>
    </div>
  );
}
