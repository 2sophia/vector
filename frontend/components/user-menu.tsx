"use client";

import { signOut, useSession } from "next-auth/react";
import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";

export function UserMenu() {
  const { data: session } = useSession();
  const email = session?.user?.email ?? "—";
  const name = session?.user?.name ?? email.split("@")[0];

  return (
    <div className="flex items-center gap-3">
      <div className="flex flex-col items-end text-right leading-tight">
        <span className="text-sm font-medium">{name}</span>
        <span className="text-xs text-zinc-500 dark:text-zinc-400">{email}</span>
      </div>
      <div className="flex size-9 items-center justify-center rounded-full bg-gradient-to-br from-indigo-500 to-purple-600 text-sm font-semibold text-white">
        {name.charAt(0).toUpperCase()}
      </div>
      <Button
        variant="ghost"
        size="icon"
        onClick={() => signOut({ callbackUrl: "/auth" })}
        aria-label="Esci"
      >
        <LogOut className="size-4" />
      </Button>
    </div>
  );
}
