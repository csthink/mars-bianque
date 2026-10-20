import { useLocation } from "react-router";

/** The login page, coming back to the current page afterwards. */
export function useLoginPath(): string {
  const { pathname, search } = useLocation();
  return `/login?next=${encodeURIComponent(pathname + search)}`;
}

/** Where to go after logging in: a path of this site only, never another origin. */
export function safeNext(next: string | null): string {
  if (next === null || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return "/";
  return next;
}
