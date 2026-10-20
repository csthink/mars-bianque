import { Link } from "react-router";
import { useInvestigations } from "../api/client";
import { Layout } from "../components/Layout";
import { Badge, Muted, Section } from "../components/ui";
import { elapsedMs, formatDateTime, formatDuration, statusLabel, triggerLabel } from "../lib/format";

export function InvestigationListPage() {
  const { data, isPending, error } = useInvestigations();
  return (
    <Layout>
      <Section title="调查" testId="investigation-list">
        {isPending && <Muted>正在加载…</Muted>}
        {error && <Muted>加载失败：{error.message}</Muted>}
        {data && data.length === 0 && <Muted>还没有调查。告警送达或回放用例后会出现在这里。</Muted>}
        {data && data.length > 0 && (
          <ul className="divide-y divide-slate-100 sm:hidden dark:divide-slate-800">
            {data.map((item) => {
              const duration = elapsedMs(item.started_at, item.finished_at);
              return (
                <li key={item.id} data-testid="investigation-card" className="py-3 first:pt-0 last:pb-0">
                  <div className="flex items-start justify-between gap-3">
                    <Link
                      to={`/investigations/${item.id}`}
                      className="font-medium text-blue-700 hover:underline dark:text-blue-300"
                    >
                      {item.alert_name ?? "手动触发"}
                    </Link>
                    <ReportBadge status={item.report_status} />
                  </div>
                  <p className="mt-1 text-sm [overflow-wrap:anywhere] text-slate-600 dark:text-slate-300">
                    {item.namespace ? `${item.namespace} / ${item.pod ?? "-"}` : "-"}
                  </p>
                  <p className="mt-1 text-xs text-slate-500 tabular-nums dark:text-slate-400">
                    {statusLabel(item.status)} · {triggerLabel(item.trigger)}
                    {item.case_id && ` ${item.case_id}`} · {formatDateTime(item.started_at)}
                    {duration !== null && ` · ${formatDuration(duration)}`}
                  </p>
                </li>
              );
            })}
          </ul>
        )}
        {data && data.length > 0 && (
          <div className="hidden overflow-x-auto sm:block">
            <table className="w-full min-w-[760px] text-left text-sm">
              <thead className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                <tr>
                  {["告警", "对象", "状态", "报告", "触发", "开始", "用时"].map((title) => (
                    <th key={title} scope="col" className="px-2 py-2 font-medium">
                      {title}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {data.map((item) => {
                  const duration = elapsedMs(item.started_at, item.finished_at);
                  return (
                    <tr key={item.id} data-testid="investigation-row" className="hover:bg-slate-50 dark:hover:bg-slate-800/50">
                      <td className="px-2 py-2.5">
                        <Link
                          to={`/investigations/${item.id}`}
                          className="font-medium text-blue-700 hover:underline dark:text-blue-300"
                        >
                          {item.alert_name ?? "手动触发"}
                        </Link>
                        <div className="font-mono text-xs text-slate-400 dark:text-slate-500">{item.id}</div>
                      </td>
                      <td className="px-2 py-2.5 [overflow-wrap:anywhere] text-slate-600 dark:text-slate-300">
                        {item.namespace ? `${item.namespace} / ${item.pod ?? "-"}` : "-"}
                      </td>
                      <td className="px-2 py-2.5 whitespace-nowrap">{statusLabel(item.status)}</td>
                      <td className="px-2 py-2.5">
                        <ReportBadge status={item.report_status} />
                      </td>
                      <td className="px-2 py-2.5 whitespace-nowrap">
                        {triggerLabel(item.trigger)}
                        {item.case_id && <span className="text-slate-500 dark:text-slate-400"> · {item.case_id}</span>}
                      </td>
                      <td className="px-2 py-2.5 whitespace-nowrap tabular-nums">{formatDateTime(item.started_at)}</td>
                      <td className="px-2 py-2.5 whitespace-nowrap tabular-nums">
                        {duration === null ? "-" : formatDuration(duration)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </Layout>
  );
}

function ReportBadge({ status }: { status: "published" | "degraded" | null }) {
  if (status === "published") return <Badge tone="success">正式报告</Badge>;
  if (status === "degraded") return <Badge tone="warning">降级报告</Badge>;
  return <span className="text-slate-400">-</span>;
}
