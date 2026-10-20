import { Link } from "react-router";
import { useEvidenceIds } from "./evidenceContext";

/** An evidence ID that jumps to the evidence list; an unknown ID is shown inert. */
export function Citation({ id }: { id: string }) {
  const known = useEvidenceIds();
  const base =
    "inline-flex items-center rounded-md px-1.5 py-0.5 font-mono text-xs whitespace-nowrap ring-1 ring-inset";
  if (!known.has(id)) {
    return (
      <span
        title="本调查没有这条证据"
        className={`${base} text-slate-400 ring-slate-200 dark:text-slate-500 dark:ring-slate-700`}
      >
        {id}
      </span>
    );
  }
  return (
    <Link
      to={{ hash: id }}
      data-citation={id}
      className={`${base} bg-blue-50 text-blue-700 ring-blue-200 hover:bg-blue-100 focus-visible:outline-2 focus-visible:outline-blue-500 dark:bg-blue-950 dark:text-blue-300 dark:ring-blue-900 dark:hover:bg-blue-900`}
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
