import type { SuggestionView } from "../api/types";
import { Citations } from "./Citation";
import { Badge, Muted, Section } from "./ui";

const STATE = {
  pending: { label: "待确认", tone: "neutral" },
  confirmed: { label: "已确认", tone: "success" },
  rejected: { label: "已驳回", tone: "danger" },
} as const;

export function SuggestionList({ suggestions }: { suggestions: SuggestionView[] }) {
  return (
    <Section title="修复建议" testId="suggestions">
      {suggestions.length === 0 ? (
        <Muted>没有修复建议。</Muted>
      ) : (
        <ul className="space-y-3">
          {suggestions.map((suggestion) => (
            <li
              key={suggestion.id}
              data-testid="suggestion"
              className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-700"
            >
              <p className="font-medium text-slate-900 dark:text-slate-100">{suggestion.action}</p>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {suggestion.command && (
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs [overflow-wrap:anywhere] text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                    {suggestion.command}
                  </code>
                )}
                <Badge tone={suggestion.risk === "change" ? "warning" : "success"}>
                  {suggestion.risk === "change" ? "变更" : "只读"}
                </Badge>
                <Badge tone={STATE[suggestion.state].tone}>{STATE[suggestion.state].label}</Badge>
              </div>
              <div className="mt-2">
                <Citations ids={suggestion.citations} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
