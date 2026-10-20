import { useState } from "react";
import { Link } from "react-router";
import { logout, useSession, useSetSession } from "../api/client";
import { useLoginPath } from "../lib/login";

/** The header's login state: who is logged in and a logout, or a link to log in. */
export function SessionMenu() {
  const session = useSession();
  const setSession = useSetSession();
  const loginPath = useLoginPath();
  const [busy, setBusy] = useState(false);
  if (session.isPending) return null;
  if (!session.data) {
    return (
      <Link to={loginPath} className="rounded-md px-2 py-1 text-sm whitespace-nowrap text-slate-200 hover:bg-slate-800 hover:text-white">
        登录
      </Link>
    );
  }
  const signOut = async () => {
    setBusy(true);
    try {
      await logout();
      setSession(null);
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="flex items-center gap-2 text-sm whitespace-nowrap">
      <span data-testid="session-user" className="text-slate-200">
        {session.data.username}
      </span>
      <button
        type="button"
        onClick={() => void signOut()}
        disabled={busy}
        className="rounded-md px-2 py-1 text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-50"
      >
        登出
      </button>
    </span>
  );
}
