import { describe, expect, it } from "vitest";
import type { InvestigationEvent } from "../../src/api/types";
import { eventLabel, stepIndex } from "../../src/lib/events";
import { plan, step } from "./fixtures";

const base = { seq: 1, created_at: "2026-10-19T22:34:00Z" };

describe("eventLabel", () => {
  const steps = stepIndex([plan(2, [step({ id: 9, seq: 1, domain: "change" })])]);

  it("names a step by its version and number", () => {
    const event: InvestigationEvent = { ...base, type: "step_finished", step_id: 9, domain: "change", status: "done" };
    expect(eventLabel(event, steps)).toBe("v2 步骤 1 change 完成");
    expect(eventLabel({ ...event, step_id: 42 }, steps)).toBe("步骤 #42 change 完成");
  });

  it("describes a rejected report by its validators and nothing else", () => {
    const event: InvestigationEvent = { ...base, type: "report_rejected", attempt: 1, violations: ["citation_integrity", "redaction"] };
    expect(eventLabel(event, steps)).toBe("报告第 1 次被拒：citation_integrity、redaction");
  });

  it("marks a judgement the deterministic rules overturned", () => {
    const event: InvestigationEvent = { ...base, type: "assessment_recorded", round: 1, sufficient: false, rule_override: true };
    expect(eventLabel(event, steps)).toBe("第 1 轮判断：不充分（确定性规则推翻了模型）");
  });
});
