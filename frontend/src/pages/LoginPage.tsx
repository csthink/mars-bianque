import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { login, UnauthorizedError, useSetSession } from "../api/client";
import { Layout } from "../components/Layout";
import { safeNext } from "../lib/login";

const field =
  "mt-1 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-blue-500 focus:outline-2 focus:outline-blue-500/40 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100";

export function LoginPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const setSession = useSetSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setSession(await login(username, password));
      void navigate(safeNext(params.get("next")), { replace: true });
    } catch (failure) {
      setError(failure instanceof UnauthorizedError ? "用户名或口令不对。" : `登录失败：${(failure as Error).message}`);
      setBusy(false);
    }
  };

  return (
    <Layout session={false}>
      <div className="mx-auto mt-6 max-w-sm sm:mt-16">
        <section
          aria-label="登录"
          className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm sm:p-6 dark:border-slate-800 dark:bg-slate-900"
        >
          <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-50">登录</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            确认修复建议与追问需要登录。演示口令在启动演示时生成并打印。
          </p>
          <form onSubmit={(event) => void submit(event)} className="mt-5 space-y-4">
            <label className="block text-sm font-medium text-slate-700 dark:text-slate-200">
              用户名
              <input
                name="username"
                autoComplete="username"
                required
                autoFocus
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                className={field}
              />
            </label>
            <label className="block text-sm font-medium text-slate-700 dark:text-slate-200">
              口令
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className={field}
              />
            </label>
            {error && (
              <p role="alert" className="text-sm text-rose-700 dark:text-rose-300">
                {error}
              </p>
            )}
            <button
              type="submit"
              disabled={busy}
              className="w-full rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:opacity-60 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
            >
              {busy ? "正在登录…" : "登录"}
            </button>
          </form>
        </section>
      </div>
    </Layout>
  );
}
