import { Link } from "react-router";
import { useLoginPath } from "../lib/login";

/** Stands in for a write control while nobody is logged in. */
export function LoginPrompt({ action }: { action: string }) {
  const path = useLoginPath();
  return (
    <p data-testid="login-prompt" className="text-sm text-slate-500 dark:text-slate-400">
      <Link to={path} className="font-medium text-blue-700 hover:underline dark:text-blue-300">
        登录
      </Link>
      后可{action}
    </p>
  );
}
