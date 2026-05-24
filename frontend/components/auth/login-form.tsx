"use client";

import { useState } from "react";
import { signIn } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2, Mail } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MicrosoftIcon } from "./microsoft-icon";

export function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const callbackUrl = searchParams.get("callbackUrl") || "/";

  const [emailOpen, setEmailOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loadingEmail, setLoadingEmail] = useState(false);
  const [loadingAzure, setLoadingAzure] = useState(false);

  async function handleEmailSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoadingEmail(true);
    setError("");
    const res = await signIn("credentials", {
      email,
      password,
      redirect: false,
    });
    if (res?.error) {
      setError("Credenziali non valide");
      setLoadingEmail(false);
    } else {
      router.push(callbackUrl);
      router.refresh();
    }
  }

  function handleAzureLogin() {
    setLoadingAzure(true);
    signIn("azure-ad", { callbackUrl });
  }

  return (
    <div className="flex w-full flex-col gap-6">
      <div className="flex flex-col gap-1.5">
        <h2 className="text-2xl font-semibold tracking-tight">Accedi</h2>
        <p className="text-sm text-zinc-500 dark:text-zinc-400">
          Sophia Vector — gestione della knowledge base.
        </p>
      </div>

      <Button
        type="button"
        size="lg"
        className="w-full font-medium"
        disabled={loadingAzure}
        onClick={handleAzureLogin}
      >
        {loadingAzure ? (
          <Loader2 className="size-4 animate-spin" />
        ) : (
          <MicrosoftIcon className="size-4" />
        )}
        {loadingAzure ? "Reindirizzamento..." : "Continua con Microsoft"}
      </Button>

      {!emailOpen ? (
        <button
          type="button"
          onClick={() => setEmailOpen(true)}
          className="mx-auto flex items-center gap-1.5 text-sm text-zinc-500 transition-colors hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
        >
          <Mail className="size-3.5" />
          oppure accedi con email
        </button>
      ) : (
        <form onSubmit={handleEmailSubmit} className="flex flex-col gap-3">
          {error && (
            <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
              {error}
            </div>
          )}
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              placeholder="alex@mwspace.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoFocus
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <Button
            type="submit"
            variant="secondary"
            className="w-full"
            disabled={loadingEmail}
          >
            {loadingEmail && <Loader2 className="size-4 animate-spin" />}
            {loadingEmail ? "Accesso in corso..." : "Accedi"}
          </Button>
          <p className="text-center text-xs text-zinc-500 dark:text-zinc-400">
            Il primo accesso con credenziali crea l&apos;account admin.
          </p>
        </form>
      )}
    </div>
  );
}
