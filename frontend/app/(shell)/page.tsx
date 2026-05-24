"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Database, FileText, Plug, ArrowRight } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";

type VectorStore = { id: string; name: string; file_counts?: { total: number } };

export default function DashboardPage() {
  const [stores, setStores] = useState<VectorStore[]>([]);
  const [sourcesCount, setSourcesCount] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const [vs, src] = await Promise.all([
          api.get<{ data: VectorStore[] }>("/vector_stores"),
          api.get<{ data: unknown[] }>("/sources").catch(() => ({ data: [] })),
        ]);
        setStores(vs.data || []);
        setSourcesCount((src.data || []).length);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const totalFiles = stores.reduce((acc, s) => acc + (s.file_counts?.total ?? 0), 0);

  const cards = [
    { label: "Vector stores", value: stores.length, icon: Database, href: "/stores" },
    { label: "File indicizzati", value: totalFiles, icon: FileText, href: "/stores" },
    { label: "Sources", value: sourcesCount, icon: Plug, href: "/sources" },
  ];

  return (
    <div className="px-8 py-10">
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <div className="flex flex-col gap-1.5">
          <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
          <p className="text-sm text-zinc-500 dark:text-zinc-400">
            Sophia Vector — gestione del database vettoriale.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {cards.map((c) => {
            const Icon = c.icon;
            return (
              <Link key={c.label} href={c.href}>
                <Card className="transition-colors hover:border-zinc-300 dark:hover:border-zinc-700">
                  <CardHeader className="pb-2">
                    <CardTitle className="flex items-center gap-2 text-sm font-medium text-zinc-500 dark:text-zinc-400">
                      <Icon className="size-4" />
                      {c.label}
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="text-3xl font-semibold tracking-tight">
                      {loading ? "—" : c.value}
                    </div>
                  </CardContent>
                </Card>
              </Link>
            );
          })}
        </div>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Vector stores</CardTitle>
          </CardHeader>
          <CardContent>
            {stores.length === 0 ? (
              <div className="flex h-24 items-center justify-center rounded-md border border-dashed border-zinc-300 text-sm text-zinc-400 dark:border-zinc-800 dark:text-zinc-600">
                {loading ? "Caricamento…" : "Nessun vector store — creane uno per iniziare."}
              </div>
            ) : (
              <div className="flex flex-col divide-y divide-zinc-100 dark:divide-zinc-800">
                {stores.map((s) => (
                  <Link
                    key={s.id}
                    href={`/stores/${s.id}`}
                    className="flex items-center justify-between py-2.5 text-sm transition-colors hover:text-indigo-600"
                  >
                    <span className="inline-flex items-center gap-2">
                      <Database className="size-4 text-zinc-400" />
                      {s.name}
                    </span>
                    <span className="inline-flex items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
                      {s.file_counts?.total ?? 0} file
                      <ArrowRight className="size-3.5" />
                    </span>
                  </Link>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
