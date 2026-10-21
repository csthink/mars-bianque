import type { CallTotals, InvestigationDetail } from "../api/types";
import { formatClock, formatDuration, formatTokens, isOpenStatus, statusLabel, triggerLabel } from "../lib/format";
import { Citation } from "./Citation";
import { Badge } from "./ui";

export function AlertCard({ detail, totals }: { detail: InvestigationDetail; totals?: CallTotals }) {
  const { investigation, alert, report } = detail;
  const labels = alert?.labels ?? {};
  const facts = [
    alert ? `${labels.namespace ?? "-"} / ${labels.pod ?? "-"}` : null,
    labels.container ? `容器 ${labels.container}` : null,
    `开始 ${formatClock(investigation.started_at)}`,
    `触发：${triggerLabel(investigation.trigger)}`,
    investigation.caller ? `调用方 ${investigation.caller}` : null,
    investigation.case_id ? `用例 ${investigation.case_id}` : null,
  ].filter((fact): fact is string => fact !== null);
  const stats = [
    `${detail.plans.length} 轮`,
    totals ? `${totals.calls} 次调用` : null,
    `${formatTokens(investigation.tokens_used)} token`,
    totals ? formatDuration(totals.elapsed_ms) : null,
  ].filter((stat): stat is string => stat !== null);

  return (
    <section
      data-testid="alert-card"
      aria-label="告警"
      className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5 dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold [overflow-wrap:anywhere] text-slate-900 sm:text-2xl dark:text-slate-50">
            {alert?.name ?? "手动触发的调查"}
          </h1>
          {alert && <Badge tone={alert.status === "firing" ? "danger" : "success"}>{alert.status}</Badge>}
          <Badge tone={isOpenStatus(investigation.status) ? "info" : "neutral"}>
            <span data-testid="investigation-status" data-status={investigation.status}>
              {statusLabel(investigation.status)}
              {isOpenStatus(investigation.status) && detail.plans.length > 0 && ` · 第 ${detail.plans.length} 轮`}
            </span>
          </Badge>
        </div>
        <ReportBadge status={report?.status ?? null} />
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-x-6 gap-y-2 text-sm text-slate-600 dark:text-slate-300">
        <p className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 sm:gap-x-2">
          {facts.map((fact, index) => (
            <span key={fact} className="[overflow-wrap:anywhere]">
              {index > 0 && <span className="mr-2 hidden text-slate-300 sm:inline dark:text-slate-600">·</span>}
              {fact}
            </span>
          ))}
          {alert?.evidence_id && (
            <span className="inline-flex items-center gap-1.5">
              <span className="hidden text-slate-300 sm:inline dark:text-slate-600">·</span>
              告警证据 <Citation id={alert.evidence_id} />
            </span>
          )}
        </p>
        <p data-testid="investigation-stats" className="tabular-nums whitespace-nowrap">
          {stats.join(" · ")}
        </p>
      </div>
      {investigation.error && (
        <p className="mt-3 text-sm text-rose-700 dark:text-rose-300">错误：{investigation.error}</p>
      )}
    </section>
  );
}

function ReportBadge({ status }: { status: "published" | "degraded" | null }) {
  if (status === "published") return <Badge tone="success">已发布正式报告</Badge>;
  if (status === "degraded") return <Badge tone="warning">降级报告</Badge>;
  return <Badge>暂无报告</Badge>;
}
