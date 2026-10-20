import type { ReactNode } from "react";
import { Link } from "react-router";
import { SessionMenu } from "./SessionMenu";

export function Layout({
  crumb,
  badges,
  session = true,
  children,
}: {
  crumb?: string;
  badges?: ReactNode;
  /** Whether the header shows the login state; the login page itself does not. */
  session?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <header className="bg-slate-900 text-slate-100 dark:border-b dark:border-slate-800">
        <div className="mx-auto flex max-w-[1440px] flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3 sm:px-6">
          <nav aria-label="位置" className="flex min-w-0 items-center gap-3 text-sm">
            <Link to="/" className="text-lg font-bold tracking-wide whitespace-nowrap text-white">
              扁鹊
            </Link>
            <Link to="/" className="whitespace-nowrap text-slate-300 hover:text-white">
              调查列表
            </Link>
            {crumb && (
              <>
                <span className="text-slate-500">/</span>
                <span className="truncate font-mono text-slate-300">{crumb}</span>
              </>
            )}
          </nav>
          <div className="flex flex-wrap items-center gap-2">
            {badges}
            {session && <SessionMenu />}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-[1440px] px-4 py-5 sm:px-6 sm:py-6">{children}</main>
    </div>
  );
}

export function HeaderChip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-full bg-slate-800 px-3 py-1 text-xs whitespace-nowrap text-slate-200 ring-1 ring-slate-700">
      {children}
    </span>
  );
}
