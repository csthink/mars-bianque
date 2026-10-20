import type { EvidenceView } from "../api/types";
import { byCollection } from "../lib/evidence";
import { formatClock } from "../lib/format";
import { Muted, Section } from "./ui";

function argumentText(args: Record<string, unknown>): string {
  return Object.entries(args)
    .filter(([, value]) => value !== null && value !== undefined && value !== false)
    .map(([key, value]) => (value === true ? key : `${key}=${String(value)}`))
    .join(" ");
}

export function EvidenceList({ evidence, highlighted }: { evidence: EvidenceView[]; highlighted: string | null }) {
  const ordered = byCollection(evidence);
  return (
    <Section
      title="证据列表（脱敏文本）"
      aside={<span className="text-xs text-slate-500 dark:text-slate-400">{evidence.length} 条，按采集时间排列</span>}
      testId="evidence-list"
    >
      {evidence.length === 0 ? (
        <Muted>还没有证据。</Muted>
      ) : (
        <ul className="divide-y divide-slate-100 dark:divide-slate-800">
          {ordered.map((item) => (
            <li
              key={item.id}
              id={item.id}
              data-testid="evidence-item"
              data-highlighted={item.id === highlighted ? "true" : undefined}
              className={`scroll-mt-4 py-3 transition-colors first:pt-0 last:pb-0 ${
                item.id === highlighted ? "-mx-2 rounded-lg bg-blue-50 px-2 ring-2 ring-blue-400 dark:bg-blue-950/60" : ""
              }`}
            >
              <p className="flex items-baseline justify-between gap-3 text-sm">
                <span className="font-mono font-semibold text-blue-700 dark:text-blue-300">{item.id}</span>
                <span className="text-xs text-slate-400 tabular-nums dark:text-slate-500">
                  {formatClock(item.collected_at)}
                </span>
              </p>
              <p className="mt-0.5 text-sm break-words text-slate-500 dark:text-slate-400">
                {item.source} · {item.tool ?? "告警原文"}
                {Object.keys(item.arguments).length > 0 && (
                  <span className="font-mono text-xs"> {argumentText(item.arguments)}</span>
                )}
              </p>
              <pre className="mt-1.5 max-h-44 overflow-auto rounded-md bg-slate-50 p-2 font-mono text-xs leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere] text-slate-700 dark:bg-slate-800/60 dark:text-slate-300">
                {item.redacted_content}
              </pre>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
