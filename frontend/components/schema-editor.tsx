"use client";

/**
 * SchemaEditor — pannello "Estrazione" riusabile su store / directory / sync.
 *
 * Il motore (GLiNER / GLiNER-relex) è zero-shot: qui l'admin sceglie solo il
 * VOCABOLARIO (tipi di entità + tipi di relazione) e se estrarre relazioni. Lo
 * schema si risolve a cascata file→directory→sync→store→default: questo pannello
 * mostra lo schema EFFETTIVO e, salvando, crea un override A QUESTO livello.
 *
 * Backend: GET/PUT (e DELETE per dir/sync) su `${basePath}/schema`.
 *   - store: GET ritorna i campi flat + `custom`
 *   - dir/sync: GET ritorna `{ own, effective }`
 */

import { useCallback, useEffect, useState } from "react";
import { Loader2, RotateCcw, Save, Sparkles, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

type Schema = {
  entity_labels: string[];
  relation_labels: string[];
  relations_enabled: boolean;
};

type SchemaResponse = Partial<Schema> & {
  custom?: boolean;
  own?: Partial<Schema> | null;
  effective?: Partial<Schema>;
};

function TagInput({
  label,
  value,
  onChange,
  disabled,
  placeholder = "aggiungi…",
}: {
  label: string;
  value: string[];
  onChange: (v: string[]) => void;
  disabled?: boolean;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const t = draft.trim();
    if (t && !value.includes(t)) onChange([...value, t]);
    setDraft("");
  };
  return (
    <div className="space-y-1.5">
      <span className="text-xs font-medium text-zinc-700 dark:text-zinc-300">{label}</span>
      <div
        className={cn(
          "flex flex-wrap gap-1.5 rounded-md border border-zinc-200 bg-white p-2 dark:border-zinc-800 dark:bg-zinc-950",
          disabled && "pointer-events-none opacity-50",
        )}
      >
        {value.map((t) => (
          <span
            key={t}
            className="inline-flex items-center gap-1 rounded bg-indigo-50 px-2 py-0.5 text-xs text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300"
          >
            {t}
            <button
              type="button"
              onClick={() => onChange(value.filter((x) => x !== t))}
              className="text-indigo-400 transition-colors hover:text-indigo-600"
              aria-label={`rimuovi ${t}`}
            >
              <X className="size-3" />
            </button>
          </span>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            } else if (e.key === "Backspace" && !draft && value.length) {
              onChange(value.slice(0, -1));
            }
          }}
          onBlur={add}
          placeholder={placeholder}
          className="min-w-[8ch] flex-1 bg-transparent text-xs outline-none placeholder:text-zinc-400"
        />
      </div>
    </div>
  );
}

export function SchemaEditor({
  basePath,
  levelLabel,
  canReset = false,
}: {
  /** path backend senza /schema, es. "/directories/dir_x", "/vector_stores/vs_x", "/ingest/sharepoint/<id>" */
  basePath: string;
  /** etichetta del livello mostrata nel testo: "store" | "directory" | "sync" */
  levelLabel: string;
  /** mostra "Ripristina ereditato" (dir/sync hanno il DELETE; lo store no) */
  canReset?: boolean;
}) {
  const [schema, setSchema] = useState<Schema | null>(null);
  const [isCustom, setIsCustom] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setMsg(null);
    try {
      const r = await api.get<SchemaResponse>(`${basePath}/schema`);
      const eff = r.effective ?? r;
      setSchema({
        entity_labels: eff.entity_labels ?? [],
        relation_labels: eff.relation_labels ?? [],
        relations_enabled: !!eff.relations_enabled,
      });
      setIsCustom(!!(r.own ?? r.custom));
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Errore di caricamento");
    } finally {
      setLoading(false);
    }
  }, [basePath]);

  useEffect(() => {
    load();
  }, [load]);

  const save = async () => {
    if (!schema) return;
    setSaving(true);
    setMsg(null);
    try {
      await api.put(`${basePath}/schema`, schema);
      setMsg("Salvato — vale dal prossimo (re-)ingest dei documenti.");
      await load();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Errore nel salvataggio");
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setSaving(true);
    setMsg(null);
    try {
      await api.delete(`${basePath}/schema`);
      setMsg("Ripristinato: eredita dal livello superiore.");
      await load();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Errore nel ripristino");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <Sparkles className="size-4 text-indigo-500" />
              Estrazione · entità &amp; relazioni
            </CardTitle>
            <CardDescription>
              Cosa estrarre per questo {levelLabel} (zero-shot). Se non personalizzato,
              eredita dai livelli superiori.
            </CardDescription>
          </div>
          <span
            className={cn(
              "shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium",
              isCustom
                ? "bg-indigo-50 text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300"
                : "bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400",
            )}
          >
            {isCustom ? "Personalizzato" : "Ereditato"}
          </span>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading || !schema ? (
          <div className="flex items-center gap-2 text-sm text-zinc-500">
            <Loader2 className="size-4 animate-spin" /> Carico…
          </div>
        ) : (
          <>
            <TagInput
              label="Tipi di entità"
              value={schema.entity_labels}
              onChange={(v) => setSchema({ ...schema, entity_labels: v })}
            />

            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={schema.relations_enabled}
                onChange={(e) => setSchema({ ...schema, relations_enabled: e.target.checked })}
                className="size-4 accent-indigo-600"
              />
              <span className="font-medium text-zinc-700 dark:text-zinc-300">
                Estrai relazioni tipizzate
              </span>
              <span className="text-[11px] text-zinc-400">(knowledge graph · GLiNER-relex)</span>
            </label>

            <TagInput
              label="Tipi di relazione"
              value={schema.relation_labels}
              onChange={(v) => setSchema({ ...schema, relation_labels: v })}
              disabled={!schema.relations_enabled}
            />

            <p className="text-[11px] text-zinc-400">
              Il modello è configurato lato server (zero-shot): qui scegli solo il{" "}
              <em>vocabolario</em>.
            </p>

            {msg && (
              <div className="rounded-md bg-zinc-50 px-3 py-2 text-xs text-zinc-600 dark:bg-zinc-900 dark:text-zinc-300">
                {msg}
              </div>
            )}

            <div className="flex items-center gap-2">
              <Button onClick={save} disabled={saving}>
                {saving ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
                Salva
              </Button>
              {canReset && isCustom && (
                <Button onClick={reset} disabled={saving} variant="outline">
                  <RotateCcw className="size-4" />
                  Ripristina ereditato
                </Button>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
