import type { InvestigationEvent, PlanView } from "../api/types";
import { statusLabel, triggerLabel } from "./format";

/** Where a step sits: its plan version and its number within the version. */
export type StepIndex = ReadonlyMap<number, { version: number; seq: number }>;

export function stepIndex(plans: PlanView[]): StepIndex {
  return new Map(plans.flatMap((plan) => plan.steps.map((step) => [step.id, { version: plan.version, seq: step.seq }])));
}

/** "v2 步骤 1"; a step the page has not read yet is named by its ID. */
export function stepName(steps: StepIndex, stepId: number): string {
  const step = steps.get(stepId);
  return step ? `v${step.version} 步骤 ${step.seq}` : `步骤 #${stepId}`;
}

const STEP_STATUS: Record<string, string> = { done: "完成", failed: "失败", running: "开始" };

/**
 * One line describing a progress event. It is made of the event's own fields: IDs,
 * states and validator names, never model text.
 */
export function eventLabel(event: InvestigationEvent, steps: StepIndex): string {
  switch (event.type) {
    case "investigation_started":
      return `调查开始（${triggerLabel(event.trigger)}）`;
    case "plan_created":
      return `计划 v${event.version} 生成（${event.source === "default" ? "默认计划" : "模型"}）`;
    case "step_started":
      return `${stepName(steps, event.step_id)} ${event.domain} 开始`;
    case "step_finished":
      return `${stepName(steps, event.step_id)} ${event.domain} ${STEP_STATUS[event.status] ?? event.status}`;
    case "evidence_added":
      return `新增证据 ${event.evidence_id}（${event.tool ?? event.source}）`;
    case "assessment_recorded":
      return `第 ${event.round} 轮判断：${event.sufficient ? "充分" : "不充分"}${event.rule_override ? "（确定性规则推翻了模型）" : ""}`;
    case "report_rejected":
      return `报告第 ${event.attempt} 次被拒：${event.violations.join("、")}`;
    case "report_published":
      return "报告通过证据门，已发布";
    case "report_degraded":
      return "报告重试用尽，发布降级报告";
    case "investigation_finished":
      return `调查结束（${statusLabel(event.status)}）`;
    case "investigation_interrupted":
      return `调查中断（${statusLabel(event.status)}）`;
  }
}
