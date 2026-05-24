import { Suspense } from "react";
import { LoginForm } from "@/components/auth/login-form";
import { VectorSpaceIllustration } from "@/components/auth/vector-illustration";
import { SophiaLogo } from "@/components/sophia-logo";

export default function AuthPage() {
  return (
    <div className="flex h-screen flex-1 flex-col overflow-hidden md:flex-row">
      {/* LEFT — login form (30%) */}
      <div className="flex w-full items-center justify-center overflow-y-auto bg-white px-8 py-12 dark:bg-zinc-950 md:w-[30%] md:min-w-[360px]">
        <div className="flex w-full max-w-sm flex-col gap-8">
          <div className="flex items-center gap-2">
            <SophiaLogo size={32} />
            <span className="text-sm font-semibold tracking-tight">
              Sophia Vector
            </span>
          </div>
          <Suspense fallback={null}>
            <LoginForm />
          </Suspense>
          <p className="text-[11px] leading-relaxed text-zinc-400 dark:text-zinc-600">
            Storage e retrieval vettoriale con API OpenAI-compatible.
            Sophia AI Cloud · accesso riservato.
          </p>
        </div>
      </div>

      {/* RIGHT — hero panel (70%) */}
      <div className="relative hidden flex-1 overflow-hidden md:flex">
        <div className="absolute inset-0 bg-gradient-to-br from-indigo-600 via-purple-600 to-fuchsia-600" />
        {/* Mesh blobs for depth */}
        <div className="absolute -left-20 top-10 size-72 rounded-full bg-cyan-400/30 blur-3xl" />
        <div className="absolute -right-10 bottom-20 size-80 rounded-full bg-pink-400/30 blur-3xl" />
        <div className="absolute right-1/3 top-1/3 size-40 rounded-full bg-amber-300/20 blur-2xl" />

        <div className="relative z-10 flex flex-1 flex-col justify-between gap-6 p-8 text-white lg:p-12">
          <div className="flex flex-col gap-3">
            <span className="inline-flex w-fit items-center gap-2 rounded-full border border-white/20 bg-white/10 px-3 py-1 text-xs font-medium backdrop-blur-md">
              <span className="size-1.5 rounded-full bg-emerald-300" />
              v0.1.0-alpha
            </span>
            <h1 className="max-w-md text-3xl font-semibold leading-tight tracking-tight">
              Vettore + grafo,
              <br />
              un retrieval solo.
            </h1>
            <p className="max-w-md text-sm leading-relaxed text-white/80">
              Hybrid search e knowledge graph sugli stessi chunk: il
              graph-augmented retrieval va oltre il solo vettoriale.
            </p>
          </div>

          <div className="flex min-h-0 flex-1 items-center justify-center py-2">
            <div className="aspect-[4/3] max-h-full w-full max-w-2xl">
              <VectorSpaceIllustration />
            </div>
          </div>

          <div className="flex flex-col gap-3 text-xs">
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-lg border border-white/15 bg-white/5 px-3 py-2.5 backdrop-blur-md">
                <div className="font-semibold">Qdrant</div>
                <div className="text-white/70">vector store</div>
              </div>
              <div className="rounded-lg border border-white/15 bg-white/5 px-3 py-2.5 backdrop-blur-md">
                <div className="font-semibold">BGE-M3</div>
                <div className="text-white/70">embeddings</div>
              </div>
              <div className="rounded-lg border border-white/15 bg-white/5 px-3 py-2.5 backdrop-blur-md">
                <div className="font-semibold">FastAPI</div>
                <div className="text-white/70">backend</div>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-lg border border-white/15 bg-white/5 px-3 py-2.5 backdrop-blur-md">
                <div className="font-semibold">FalkorDB</div>
                <div className="text-white/70">knowledge graph</div>
              </div>
              <div className="rounded-lg border border-white/15 bg-white/5 px-3 py-2.5 backdrop-blur-md">
                <div className="font-semibold">GLiNER</div>
                <div className="text-white/70">entity NER · zero-shot</div>
              </div>
              <div className="rounded-lg border border-white/15 bg-white/5 px-3 py-2.5 backdrop-blur-md">
                <div className="font-semibold">Docling</div>
                <div className="text-white/70">doc parser · IBM</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
