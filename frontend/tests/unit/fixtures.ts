import type { CallList, CallView, EvidenceView } from "../../src/api/types";

export function call(overrides: Partial<CallView>): CallView {
  return {
    id: 1,
    agent: "investigation",
    kind: "model",
    name: "plan",
    caller: null,
    started_at: "2026-10-19T22:34:00Z",
    latency_ms: 1000,
    status: "ok",
    retries: 0,
    input_summary: null,
    output_summary: null,
    error: null,
    evidence_ids: [],
    tokens_in: 100,
    tokens_out: 20,
    gate: { passed: true, validators: [] },
    ...overrides,
  };
}

export function evidence(overrides: Partial<EvidenceView>): EvidenceView {
  return {
    id: "ev_0000000001",
    step_id: null,
    call_id: null,
    source: "kubernetes",
    tool: "k8s_list_pods",
    collected_at: "2026-10-19T22:34:00Z",
    redacted_content: "{}",
    arguments: {},
    ...overrides,
  };
}

export const callList: CallList = {
  calls: [
    call({ id: 1 }),
    call({
      id: 2,
      agent: "kubernetes",
      kind: "tool",
      name: "k8s_get_pod_status",
      started_at: "2026-10-19T22:34:03Z",
      latency_ms: 210,
      input_summary: '{"pod": "x"}',
      output_summary: "{}",
      evidence_ids: ["ev_0000000002"],
      tokens_in: null,
      tokens_out: null,
      gate: null,
    }),
    call({
      id: 4,
      kind: "knowledge",
      name: "retrieve_knowledge",
      started_at: "2026-10-19T22:34:00.100Z",
      latency_ms: 180,
      input_summary: "Bianque Pod Restarting",
      output_summary: "kn_c73d8439117f CrashLoopBackOff > Reading the previous container log",
      tokens_in: null,
      tokens_out: null,
      gate: null,
    }),
    call({ id: 3, name: "report", started_at: "2026-10-19T22:34:30Z", gate: { passed: false, validators: ["citation_integrity"] } }),
  ],
  totals: {
    calls: 4,
    model_calls: 2,
    tool_calls: 1,
    knowledge_calls: 1,
    tokens_in: 38000,
    tokens_out: 210,
    latency_ms: 2390,
    elapsed_ms: 84000,
  },
};
