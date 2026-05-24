import { Sidebar } from "@/components/sidebar";
import { UserMenu } from "@/components/user-menu";

export default function ShellLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-screen bg-zinc-50 dark:bg-zinc-950">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <header className="flex h-16 shrink-0 items-center justify-end border-b border-zinc-200 bg-white/80 px-8 backdrop-blur-md dark:border-zinc-800 dark:bg-zinc-950/80">
          <UserMenu />
        </header>
        <main className="flex-1 overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
