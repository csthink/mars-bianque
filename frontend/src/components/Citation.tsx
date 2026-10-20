import { Link } from "react-router";
import { useCitableIds } from "./citableContext";

/**
 * An evidence ID that jumps to the evidence list, or a suggestion ID that jumps to the
 * suggestion; an unknown ID is shown inert.
 */
export function Citation({ id }: { id: string }) {
  const known = useCitableIds();
  const base =
    "inline-flex items-center rounded-md px-1.5 py-0.5 font-mono text-xs whitespace-nowrap ring-1 ring-inset";
  if (!known.has(id)) {
    return (
      <span
        title={id.startsWith("sg_") ? "本调查没有这条修复建议" : "本调查没有这条证据"}
        className={`${base} text-slate-400 ring-slate-200 dark:text-slate-500 dark:ring-slate-700`}
      >
        {id}
      </span>
    );
  }
  const tone = id.startsWith("sg_")
    ? "bg-violet-50 text-violet-700 ring-violet-200 hover:bg-violet-100 dark:bg-violet-950 dark:text-violet-300 dark:ring-violet-900 dark:hover:bg-violet-900"
    : "bg-blue-50 text-blue-700 ring-blue-200 hover:bg-blue-100 dark:bg-blue-950 dark:text-blue-300 dark:ring-blue-900 dark:hover:bg-blue-900";
  return (
    <Link
      to={{ hash: id }}
      data-citation={id}
      title={id.startsWith("sg_") ? "修复建议" : undefined}
      className={`${base} ${tone} focus-visible:outline-2 focus-visible:outline-blue-500`}
    >
      {id}
    </Link>
  );
}

export function Citations({ ids }: { ids: string[] }) {
  if (ids.length === 0) return null;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {ids.map((id) => (
        <Citation key={id} id={id} />
      ))}
    </span>
  );
}
