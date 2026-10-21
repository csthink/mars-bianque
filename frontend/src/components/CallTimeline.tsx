import { useMemo, useState } from "react";
import type { CallView } from "../api/types";
import { agentLabel, formatDuration, formatTokens } from "../lib/format";
import { buildTimeline } from "../lib/timeline";
import { Citations } from "./Citation";
import { agentColor } from "../lib/agents";
import { Badge, Muted, Section } from "./ui";

/** Knowledge retrieval and skill loading are drawn in their own colors on the investigation agent's lane. */
const KNOWLEDGE_BAR = "bg-violet-600 dark:bg-violet-400";
const SKILL_BAR = "bg-fuchsia-600 dark:bg-fuchsia-400";

/** The output summary of a skill call ends with how the skill was loaded. */
const SKILL_LOADS: Record<string, string> = {
  "first load": "首次加载",
  "already loaded": "已加载（本次调查之前加载过）",
};

function skillLoad(summary: string | null): { digest: string; load: string } {
  const [digest = "", how = ""] = (summary ?? "").split("; ");
  return { digest, load: SKILL_LOADS[how] ?? how };
}

export function CallTimeline({
  calls,
  startedAt,
  elapsedMs,
}: {
  calls: CallView[];
  startedAt: string;
  elapsedMs: number;
}) {
  const timeline = useMemo(() => buildTimeline(calls, startedAt, elapsedMs), [calls, startedAt, elapsedMs]);
  const [selectedId, setSelectedId] = useState<number | null>(
    () => calls.find((call) => call.kind === "tool")?.id ?? calls[0]?.id ?? null,
  );
  const selected = calls.find((call) => call.id === selectedId);
  const percent = (seconds: number) => `${(seconds / timeline.axisEnd) * 100}%`;

  return (
    <Section
      title="调用时间线（按 Agent 分组）"
      aside={<span className="text-xs text-slate-500 dark:text-slate-400">横轴 0 至 {timeline.axisEnd} 秒</span>}
      testId="call-timeline"
    >
      {calls.length === 0 ? (
        <Muted>还没有调用。</Muted>
      ) : (
        <>
          <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-3 gap-y-2 sm:grid-cols-[7rem_minmax(0,1fr)]">
            <div />
            <div className="relative h-4 text-[11px] text-slate-400 tabular-nums dark:text-slate-500">
              {timeline.ticks.map((tick, index) => (
                <span
                  key={tick}
                  className={`absolute whitespace-nowrap ${index === 0 ? "" : index === timeline.ticks.length - 1 ? "-translate-x-full" : "-translate-x-1/2"}`}
                  style={{ left: percent(tick) }}
                >
                  {tick}
                </span>
              ))}
            </div>
            {timeline.rows.map((row) => {
              const color = agentColor(row.agent);
              return (
                <div key={row.agent} className="contents" data-testid="timeline-row" data-agent={row.agent}>
                  <div className={`truncate text-sm font-medium leading-7 ${color.text}`}>{agentLabel(row.agent)}</div>
                  <div className={`relative h-7 rounded-md ${color.lane}`}>
                    {timeline.ticks.slice(1, -1).map((tick) => (
                      <span
                        key={tick}
                        aria-hidden
                        className="absolute top-0 h-full w-px bg-slate-200 dark:bg-slate-700"
                        style={{ left: percent(tick) }}
                      />
                    ))}
                    {row.bars.map((bar) => (
                      <button
                        key={bar.call.id}
                        type="button"
                        aria-label={`${bar.call.name}，${agentLabel(bar.call.agent)}，${formatDuration(bar.call.latency_ms ?? 0)}`}
                        aria-pressed={bar.call.id === selectedId}
                        onClick={() => setSelectedId(bar.call.id)}
                        className={`absolute top-1.5 h-4 rounded-sm ${
                          bar.call.status !== "ok"
                            ? "bg-rose-600 dark:bg-rose-400"
                            : bar.call.kind === "knowledge"
                              ? KNOWLEDGE_BAR
                              : bar.call.kind === "skill"
                                ? SKILL_BAR
                                : color.bar
                        } ${bar.call.kind === "model" ? "min-w-[6px] opacity-40 hover:opacity-60" : "z-10 min-w-[8px]"} ${
                          bar.call.id === selectedId
                            ? "z-20 opacity-100 ring-2 ring-slate-900 ring-offset-1 dark:ring-white dark:ring-offset-slate-900"
                            : ""
                        }`}
                        style={{ left: percent(bar.start), width: percent(Math.max(bar.end - bar.start, 0)) }}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
          <p className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden className="h-2.5 w-4 rounded-sm bg-slate-500 dark:bg-slate-300" />
              工具调用
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden className="h-2.5 w-4 rounded-sm bg-slate-500 opacity-40 dark:bg-slate-300 dark:opacity-50" />
              模型调用
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden className={`h-2.5 w-4 rounded-sm ${KNOWLEDGE_BAR}`} />
              知识检索
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden className={`h-2.5 w-4 rounded-sm ${SKILL_BAR}`} />
              skill 加载
            </span>
            <span>点一次调用查看详情</span>
          </p>
          {selected && <CallDetail call={selected} />}
        </>
      )}
    </Section>
  );
}

function CallDetail({ call }: { call: CallView }) {
  return (
    <div
      data-testid="call-detail"
      className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm dark:border-slate-700 dark:bg-slate-800/60"
    >
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-slate-800 dark:text-slate-200">
        <span className="font-mono font-medium">{call.name}</span>
        <span className="text-slate-500 dark:text-slate-400">（{agentLabel(call.agent)}）</span>
        <span>· {formatDuration(call.latency_ms ?? 0)}</span>
        <span>· {call.status === "ok" ? "成功" : call.status}</span>
        <span>· 重试 {call.retries} 次</span>
        {call.kind === "model" && (
          <span>
            · token {formatTokens(call.tokens_in ?? 0)} 入 / {formatTokens(call.tokens_out ?? 0)} 出
          </span>
        )}
        {call.gate && (
          <Badge tone={call.gate.passed ? "success" : "danger"}>
            {call.gate.passed ? "证据门通过" : `证据门拒绝：${call.gate.validators.join("、")}`}
          </Badge>
        )}
        {call.evidence_ids.length > 0 && (
          <span className="inline-flex items-center gap-1">
            · 证据 <Citations ids={call.evidence_ids} />
          </span>
        )}
      </p>
      {call.kind === "skill" ? (
        <SkillDetail call={call} />
      ) : call.kind !== "model" ? (
        <dl className="mt-2 space-y-2">
          <div>
            <dt className="text-xs text-slate-500 dark:text-slate-400">{call.kind === "knowledge" ? "检索查询" : "参数"}</dt>
            <dd className="font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere] text-slate-700 dark:text-slate-300">
              {call.input_summary}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500 dark:text-slate-400">
              {call.error ? "错误" : call.kind === "knowledge" ? "检索到的运维手册（知识不是证据，只能列为参考资料）" : "结果摘要"}
            </dt>
            <dd>
              <pre className="mt-1 max-h-48 overflow-auto rounded bg-white p-2 font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere] text-slate-700 dark:bg-slate-900 dark:text-slate-300">
                {call.error ?? call.output_summary}
              </pre>
            </dd>
          </div>
        </dl>
      ) : (
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          模型调用只显示耗时、token 与证据门结果，不显示输入与输出。
        </p>
      )}
    </div>
  );
}

function SkillDetail({ call }: { call: CallView }) {
  const { digest, load } = skillLoad(call.output_summary);
  return (
    <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
      <dt className="text-xs leading-5 text-slate-500 dark:text-slate-400">skill 名</dt>
      <dd className="font-mono text-xs leading-5 [overflow-wrap:anywhere] text-slate-700 dark:text-slate-300">
        {call.input_summary}
      </dd>
      <dt className="text-xs leading-5 text-slate-500 dark:text-slate-400">加载情况</dt>
      <dd className="text-xs leading-5 text-slate-700 dark:text-slate-300">{load}</dd>
      <dt className="text-xs leading-5 text-slate-500 dark:text-slate-400">正文摘要</dt>
      <dd className="font-mono text-xs leading-5 text-slate-700 dark:text-slate-300">{digest}</dd>
      <dd className="col-span-2 mt-1 text-xs text-slate-500 dark:text-slate-400">
        skill 只说明怎么查，不是证据，也不能列为参考资料。
      </dd>
    </dl>
  );
}
