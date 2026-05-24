"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutDashboard, Database, Plug, Search } from "lucide-react";
import { SophiaLogo } from "@/components/sophia-logo";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/stores", label: "Vector Stores", icon: Database },
  { href: "/sources", label: "Sources", icon: Plug },
  { href: "/search", label: "Search", icon: Search },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="flex h-screen w-60 shrink-0 flex-col border-r border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
      <div className="flex items-center gap-3 px-5 py-5">
        <SophiaLogo size={32} />
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-semibold tracking-tight">Sophia Vector</span>
          <span className="text-[10px] text-zinc-500 dark:text-zinc-400">
            v0.1.0-alpha
          </span>
        </div>
      </div>

      <nav className="flex flex-1 flex-col gap-1 px-3 py-2">
        {NAV.map((item) => {
          const Icon = item.icon;
          const active =
            item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-zinc-100 font-medium text-zinc-900 dark:bg-zinc-800 dark:text-zinc-50"
                  : "text-zinc-600 hover:bg-zinc-50 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-900 dark:hover:text-zinc-100",
              )}
            >
              <Icon className="size-4" />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="border-t border-zinc-200 px-5 py-4 text-[11px] text-zinc-400 dark:border-zinc-800 dark:text-zinc-600">
        data management
      </div>
    </aside>
  );
}
