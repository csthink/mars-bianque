import { useEffect, useState } from "react";
import type { AssessmentView, PlanView } from "../api/types";
import { categoryLabel } from "../lib/format";
import { Citations } from "./Citation";
import { agentColor } from "../lib/agents";
import { Badge, Muted, Section } from "./ui";

const STEP_STATUS: Record<string, { label: string; className: string }> = {
  done: { label: "完成", className: "text-emerald-700 dark:text-emerald-400" },
  running: { label: "进行中", className: "text-blue-700 dark:text-blue-400" },
  failed: { label: "失败", className: "text-rose-700 dark:text-rose-400" },
  pending: { label: "等待", className: "text-slate-500 dark:text-slate-400" },
};

/** A step picked in the flow graph; ``at`` makes picking the same step again count. */
export interface StepFocus {
  stepId: number;
  version: number;
  at: number;
}

export function PlanPanel({
  plans,
  assessments,
  focus = null,
}: {
  plans: PlanView[];
  assessments: AssessmentView[];
  focus?: StepFocus | null;
}) {
  const [version, setVersion] = useState(plans[0]?.version ?? 1);
  // A newly picked step shows its version; picking a tab afterwards still works.
  const [shownFocus, setShownFocus] = useState(focus);
  if (focus !== shownFocus) {
    setShownFocus(focus);
    if (focus) setVersion(focus.version);
  }
  useEffect(() => {
    if (focus) document.getElementById(`plan-step-${focus.stepId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [focus]);
  const plan = plans.find((item) => item.version === version);
  const nextPlan = plans.find((item) => item.version === version + 1);

  const tabs = (
    <div role="tablist" aria-label="计划版本" className="flex gap-1">
      {plans.map((item) => (
        <button
          key={item.version}
          type="button"
          role="tab"
          aria-selected={item.version === version}
          onClick={() => setVersion(item.version)}
          className={`rounded-full px-3 py-1 text-xs font-medium transition ${
            item.version === version
              ? "bg-slate-800 text-white dark:bg-slate-100 dark:text-slate-900"
              : "text-slate-600 ring-1 ring-slate-300 ring-inset hover:bg-slate-100 dark:text-slate-300 dark:ring-slate-700 dark:hover:bg-slate-800"
          }`}
        >
          v{item.version}
        </button>
      ))}
    </div>
  );

  return (
    <Section title="计划与判断" aside={plans.length > 0 ? tabs : undefined} testId="plan-panel">
      {plan === undefined ? (
        <Muted>还没有计划。</Muted>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
          <div role="tabpanel" aria-label={`计划 v${plan.version}`} className="min-w-0">
            <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
              计划 v{plan.version}（{plan.source === "default" ? "模型的计划被拒，退回默认计划" : "模型生成"}）：
              {plan.rationale}
            </p>
            {plan.skills.length > 0 && (
              <p data-testid="plan-skills" className="-mt-1 mb-3 flex flex-wrap items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
                <span className="mr-0.5">参考的 skill</span>
                {plan.skills.map((name) => (
                  <Badge key={name}>
                    <span className="font-mono">{name}</span>
                  </Badge>
                ))}
              </p>
            )}
            <ol className="space-y-2.5">
              {plan.steps.map((step) => {
                const status = STEP_STATUS[step.status] ?? { label: step.status, className: "" };
                return (
                  <li
                    key={step.id}
                    id={`plan-step-${step.id}`}
                    data-testid="plan-step"
                    data-focused={focus?.stepId === step.id ? "true" : undefined}
                    className={`scroll-mt-4 text-sm ${
                      focus?.stepId === step.id ? "-mx-2 rounded-lg bg-blue-50 px-2 py-1 ring-2 ring-blue-400 dark:bg-blue-950/60" : ""
                    }`}
                  >
                    {/* Narrow screens: domain and status on one row, the goal below at full
                        width. From sm up: domain column, goal, status. */}
                    <div className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-x-2 gap-y-1 sm:grid-cols-[5.5rem_minmax(0,1fr)_auto]">
                      <span className="col-start-1 row-start-1">
                        <Badge tone={agentColor(step.domain).chip}>{step.domain}</Badge>
                      </span>
                      <span className="col-span-3 row-start-2 min-w-0 text-slate-800 sm:col-span-1 sm:col-start-2 sm:row-start-1 dark:text-slate-200">
                        {step.goal}
                      </span>
                      <span className={`col-start-3 row-start-1 justify-self-end text-xs font-medium ${status.className}`}>
                        {status.label}
                      </span>
                    </div>
                    {step.findings.map((finding) => (
                      <p
                        key={finding.statement}
                        className="mt-1 border-l-2 sm:ml-[6rem] border-slate-200 pl-3 text-xs leading-relaxed text-slate-500 dark:border-slate-700 dark:text-slate-400"
                      >
                        {finding.statement} <Citations ids={finding.citations} />
                      </p>
                    ))}
                  </li>
                );
              })}
            </ol>
            {nextPlan && (
              <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
                切到 v{nextPlan.version} 看补充的 {nextPlan.steps.length} 个步骤
              </p>
            )}
          </div>
          <div className="space-y-3">
            {assessments.length === 0 && <Muted>还没有判断。</Muted>}
            {assessments.map((assessment) => (
              <AssessmentCard key={assessment.round} assessment={assessment} />
            ))}
          </div>
        </div>
      )}
    </Section>
  );
}

function AssessmentCard({ assessment }: { assessment: AssessmentView }) {
  const tone = assessment.sufficient
    ? "border-emerald-200 bg-emerald-50/60 dark:border-emerald-900 dark:bg-emerald-950/40"
    : "border-amber-200 bg-amber-50/60 dark:border-amber-900 dark:bg-amber-950/40";
  return (
    <article data-testid="assessment" className={`rounded-lg border p-3 text-sm ${tone}`}>
      <header className="mb-1.5 flex items-center gap-2">
        <h3 className="font-semibold text-slate-900 dark:text-slate-100">第 {assessment.round} 轮</h3>
        <Badge tone={assessment.sufficient ? "success" : "warning"}>
          {assessment.sufficient ? "充分" : "不充分"}
        </Badge>
      </header>
      <dl className="space-y-1 text-slate-700 dark:text-slate-300">
        {assessment.hypothesis_category && (
          <div className="flex gap-1">
            <dt className="shrink-0">假设：</dt>
            <dd>
              {categoryLabel(assessment.hypothesis_category)}（{assessment.hypothesis_category}）
            </dd>
          </div>
        )}
        <div className="flex gap-1">
          <dt className="shrink-0">理由：</dt>
          <dd>{assessment.reason}</dd>
        </div>
        {assessment.missing.length > 0 && (
          <div className="flex gap-1">
            <dt className="shrink-0">缺：</dt>
            <dd>{assessment.missing.join("；")}</dd>
          </div>
        )}
        <div className="flex gap-1">
          <dt className="shrink-0">确定性规则：</dt>
          <dd>{assessment.rule_override ? "推翻了模型的结论" : "与模型结论一致"}</dd>
        </div>
      </dl>
    </article>
  );
}
