import type { CallList } from "../api/types";
import { agentLabel, formatDuration, formatTokens } from "../lib/format";
import { Citations } from "./Citation";
import { Muted, Section } from "./ui";

const KIND_LABELS: Record<CallList["calls"][number]["kind"], string> = {
  model: "模型",
  tool: "工具",
  knowledge: "知识检索",
  skill: "skill",
};

export function CallTable({ data, startedAt }: { data: CallList; startedAt: string }) {
  const { calls, totals } = data;
  const origin = new Date(startedAt).getTime();
  const summary = `合计 ${totals.calls} 次调用（模型 ${totals.model_calls} 次、工具 ${totals.tool_calls} 次、知识检索 ${totals.knowledge_calls} 次、skill ${totals.skill_calls} 次）· token ${formatTokens(
    totals.tokens_in + totals.tokens_out,
  )} · 总耗时 ${formatDuration(totals.elapsed_ms)}`;
  return (
    <Section title="调用记录" testId="call-table">
      <p data-testid="call-totals" className="mb-3 text-sm text-slate-600 tabular-nums dark:text-slate-300">
        {summary}
      </p>
      {calls.length === 0 ? (
        <Muted>还没有调用。</Muted>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-left text-sm">
            <thead className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
              <tr>
                {["开始", "Agent", "类型", "名称", "耗时", "token", "状态", "证据门", "证据"].map((title) => (
                  <th key={title} scope="col" className="px-2 py-2 font-medium whitespace-nowrap">
                    {title}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {calls.map((call) => (
                <tr key={call.id} data-testid="call-row" className="text-slate-700 dark:text-slate-300">
                  <td className="px-2 py-1.5 font-mono text-xs whitespace-nowrap tabular-nums">
                    +{((new Date(call.started_at).getTime() - origin) / 1000).toFixed(1)} 秒
                  </td>
                  <td className="px-2 py-1.5 whitespace-nowrap">{agentLabel(call.agent)}</td>
                  <td className="px-2 py-1.5 whitespace-nowrap">{KIND_LABELS[call.kind]}</td>
                  <td className="px-2 py-1.5 font-mono text-xs whitespace-nowrap">{call.name}</td>
                  <td className="px-2 py-1.5 whitespace-nowrap tabular-nums">{formatDuration(call.latency_ms ?? 0)}</td>
                  <td className="px-2 py-1.5 whitespace-nowrap tabular-nums">
                    {call.kind === "model" ? formatTokens((call.tokens_in ?? 0) + (call.tokens_out ?? 0)) : "-"}
                  </td>
                  <td className="px-2 py-1.5 whitespace-nowrap">{call.status === "ok" ? "成功" : call.status}</td>
                  <td className="px-2 py-1.5 whitespace-nowrap">
                    {call.gate === null ? "-" : call.gate.passed ? "通过" : `拒绝：${call.gate.validators.join("、")}`}
                  </td>
                  <td className="px-2 py-1.5">
                    {call.evidence_ids.length === 0 ? "-" : <Citations ids={call.evidence_ids} />}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
