import type { RejectedAttempt, ReportView } from "../api/types";
import { categoryLabel, confidenceLabel, formatClock } from "../lib/format";
import { Citations } from "./Citation";
import { Badge, Muted, Section } from "./ui";

/** A field path that wraps after "." and before "[" instead of inside a name. */
function breakablePath(path: string) {
  return path.split(/(?<=\.)|(?=\[)/).map((part, index) => (
    <span key={index}>
      {index > 0 && <wbr />}
      {part}
    </span>
  ));
}

function Subheading({ children }: { children: string }) {
  return <h3 className="mb-2 text-sm font-semibold text-slate-900 dark:text-slate-100">{children}</h3>;
}

export function ReportPanel({ report, rejected }: { report: ReportView | null; rejected: RejectedAttempt[] }) {
  const badge =
    report === null ? undefined : report.status === "published" ? (
      <Badge tone="success">已通过证据门 · 7 项校验</Badge>
    ) : (
      <Badge tone="warning">降级报告：重试已用尽，根因未知</Badge>
    );
  return (
    <Section title="报告" aside={badge} testId="report">
      {report === null ? <Muted>还没有报告。</Muted> : <ReportBody report={report} />}
      {rejected.length > 0 && (
        <ul className="mt-4 space-y-1 text-xs text-slate-500 dark:text-slate-400" data-testid="rejected-attempts">
          {rejected.map((attempt) => (
            <li key={attempt.attempt}>
              第 {attempt.attempt} 次生成被拒：{attempt.validators.join("、") || "未记录校验器"}（只显示原因）
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function ReportBody({ report }: { report: ReportView }) {
  const { body } = report;
  const cause = body.root_cause;
  return (
    <div className="space-y-5 text-sm text-slate-700 dark:text-slate-300">
      <p className="leading-relaxed text-slate-800 dark:text-slate-200">{body.summary}</p>

      <div
        data-testid="root-cause"
        className="rounded-lg border border-emerald-200 bg-emerald-50/60 p-3 dark:border-emerald-900 dark:bg-emerald-950/40"
      >
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h3 className="font-semibold text-slate-900 dark:text-slate-100">根因</h3>
          {cause.status === "identified" && cause.category ? (
            <Badge tone="success">{categoryLabel(cause.category)}</Badge>
          ) : (
            <Badge tone="warning">未知</Badge>
          )}
          <Badge tone="neutral">置信度 {confidenceLabel(cause.confidence)}</Badge>
        </div>
        {cause.object && (
          <dl className="mb-1.5 font-mono text-xs text-slate-600 dark:text-slate-400">
            <dd>
              {cause.object.kind} {cause.object.namespace}/{cause.object.name}
            </dd>
            {cause.object.field && <dd className="[overflow-wrap:anywhere]">字段 {breakablePath(cause.object.field)}</dd>}
          </dl>
        )}
        <p className="leading-relaxed text-slate-800 dark:text-slate-200">{cause.statement}</p>
        <div className="mt-2">
          <Citations ids={cause.citations} />
        </div>
      </div>

      <div>
        <Subheading>影响</Subheading>
        <p>{body.impact}</p>
      </div>

      <div>
        <Subheading>时间线</Subheading>
        <ol className="space-y-2">
          {body.timeline.map((entry) => (
            <li key={`${entry.time}-${entry.event}`} className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-2">
              <span className="font-mono text-xs leading-5 text-slate-500 tabular-nums dark:text-slate-400">
                {formatClock(entry.time)}
              </span>
              <span>
                {entry.event} <Citations ids={entry.citations} />
              </span>
            </li>
          ))}
        </ol>
      </div>

      <div>
        <Subheading>{`结论（${body.claims.length} 条）`}</Subheading>
        <ul className="space-y-2">
          {body.claims.map((claim) => (
            <li key={claim.statement} data-testid="claim">
              {claim.statement} <Citations ids={claim.citations} />
            </li>
          ))}
        </ul>
      </div>

      <div>
        <Subheading>缺失证据</Subheading>
        {body.missing_evidence.length === 0 ? (
          <p className="text-slate-500 dark:text-slate-400">无</p>
        ) : (
          <ul className="list-disc space-y-1 pl-5">
            {body.missing_evidence.map((item) => (
              <li key={item.what}>
                {item.what}：{item.reason}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div>
        <Subheading>参考资料（知识，不作为证据）</Subheading>
        {body.references.length === 0 ? (
          <p className="text-slate-500 dark:text-slate-400">无</p>
        ) : (
          <ul className="space-y-1">
            {body.references.map((ref) => (
              <li key={ref.knowledge_id} className="flex flex-wrap items-center gap-2">
                <Badge>{ref.knowledge_id}</Badge>
                {ref.title}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
