// Response shapes of the read interface (backend/bianque/api/schemas.py).

export type ReportStatus = "published" | "degraded";

export interface InvestigationSummary {
  id: string;
  status: string;
  trigger: string;
  case_id: string | null;
  alert_name: string | null;
  namespace: string | null;
  pod: string | null;
  started_at: string;
  finished_at: string | null;
  report_status: ReportStatus | null;
}

export interface InvestigationInfo {
  id: string;
  status: string;
  trigger: string;
  case_id: string | null;
  tool_backend: string;
  llm_mode: string;
  llm_model: string | null;
  token_budget: number;
  tokens_used: number;
  started_at: string;
  finished_at: string | null;
  error: string | null;
}

export interface AlertView {
  name: string | null;
  status: string;
  fingerprint: string;
  labels: Record<string, string>;
  annotations: Record<string, string>;
  starts_at: string;
  received_at: string;
  evidence_id: string | null;
}

export interface Finding {
  statement: string;
  citations: string[];
}

export interface StepView {
  id: number;
  seq: number;
  domain: string;
  goal: string;
  agent: string;
  suggested_tools: string[];
  expected_evidence: string[];
  status: string;
  findings: Finding[];
  started_at: string | null;
  finished_at: string | null;
}

export interface PlanView {
  id: number;
  version: number;
  source: string;
  rationale: string;
  created_at: string;
  steps: StepView[];
}

export interface AssessmentView {
  round: number;
  sufficient: boolean;
  reason: string;
  hypothesis_category: string | null;
  missing: string[];
  rule_override: boolean;
  created_at: string;
}

export interface EvidenceView {
  id: string;
  step_id: number | null;
  call_id: number | null;
  source: string;
  tool: string | null;
  collected_at: string;
  redacted_content: string;
  arguments: Record<string, unknown>;
}

export interface Cited {
  citations: string[];
}

export interface RootCause extends Cited {
  status: "identified" | "unknown";
  category: string | null;
  object: { kind: string; namespace: string; name: string; field: string | null } | null;
  statement: string;
  confidence: "high" | "medium" | "low";
}

export interface ReportBody {
  schema_version: string;
  summary: string;
  impact: string;
  timeline: (Cited & { time: string; event: string })[];
  root_cause: RootCause;
  claims: (Cited & { statement: string })[];
  missing_evidence: { what: string; reason: string }[];
  suggestions: (Cited & { action: string; command: string | null; risk: string })[];
  references: { knowledge_id: string; title: string }[];
  followup_questions: string[];
}

export interface ReportView {
  id: number;
  attempt: number;
  status: ReportStatus;
  body: ReportBody;
  created_at: string;
}

export interface RejectedAttempt {
  attempt: number;
  validators: string[];
  created_at: string;
}

export interface SuggestionView {
  id: string;
  seq: number;
  action: string;
  command: string | null;
  risk: string;
  citations: string[];
  state: "pending" | "confirmed" | "rejected";
  decided_by: string | null;
  decided_at: string | null;
  decision_comment: string | null;
}

export interface InvestigationDetail {
  investigation: InvestigationInfo;
  alert: AlertView | null;
  plans: PlanView[];
  assessments: AssessmentView[];
  evidence: EvidenceView[];
  report: ReportView | null;
  rejected_attempts: RejectedAttempt[];
  suggestions: SuggestionView[];
}

export interface CallView {
  id: number;
  agent: string;
  kind: "model" | "tool" | "knowledge";
  name: string;
  caller: string | null;
  started_at: string;
  latency_ms: number | null;
  status: string;
  retries: number;
  input_summary: string | null;
  output_summary: string | null;
  error: string | null;
  evidence_ids: string[];
  tokens_in: number | null;
  tokens_out: number | null;
  gate: { passed: boolean; validators: string[] } | null;
}

export interface CallTotals {
  calls: number;
  model_calls: number;
  tool_calls: number;
  knowledge_calls: number;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
  elapsed_ms: number;
}

export interface CallList {
  calls: CallView[];
  totals: CallTotals;
}

// Login, follow-up sessions and suggestion decisions.

export interface SessionUser {
  username: string;
}

export interface FollowupSessionView {
  id: number;
  investigation_id: string;
  created_by: string;
  created_at: string;
}

export interface FollowupSessionSummary extends FollowupSessionView {
  turns: number;
}

export interface AnswerSegment {
  text: string;
  citations: string[];
  insufficient_evidence: boolean;
}

export interface FollowupMessage {
  id: number;
  seq: number;
  role: "user" | "assistant";
  content: string;
  segments: AnswerSegment[];
  citations: string[];
  references: string[];
  unknown: boolean;
  created_at: string;
}

export interface FollowupSessionDetail {
  session: FollowupSessionView;
  messages: FollowupMessage[];
}

export interface QuestionList {
  recorded_only: boolean;
  questions: { id: string; question: string }[];
}

// Server-sent events. Progress events carry IDs, states and validator
// names only; the page reads the content from the detail interface.

export const TERMINAL_EVENTS = ["investigation_finished", "investigation_interrupted"] as const;

export type InvestigationEvent = { seq: number; created_at: string } & (
  | { type: "investigation_started"; trigger: string }
  | { type: "plan_created"; plan_id: number; version: number; source: string }
  | { type: "step_started" | "step_finished"; step_id: number; domain: string; status: string }
  | { type: "evidence_added"; evidence_id: string; source: string; tool: string | null }
  | { type: "assessment_recorded"; round: number; sufficient: boolean; rule_override: boolean }
  | { type: "report_rejected"; attempt: number; violations: string[] }
  | { type: "report_published" | "report_degraded"; report_id: number }
  | { type: (typeof TERMINAL_EVENTS)[number]; status: string }
);

export type InvestigationEventType = InvestigationEvent["type"];

export const INVESTIGATION_EVENT_TYPES: readonly InvestigationEventType[] = [
  "investigation_started",
  "plan_created",
  "step_started",
  "step_finished",
  "evidence_added",
  "assessment_recorded",
  "report_rejected",
  "report_published",
  "report_degraded",
  ...TERMINAL_EVENTS,
];

export type ProgressStage = "retrieving" | "generating" | "validating" | "retrying";

export type FailureReason = "gate_rejected" | "model_unavailable" | "model_error" | "budget_exhausted";

/** An event of a follow-up answer stream. No event but ``content`` carries model text. */
export type FollowupEvent =
  | { event: "progress"; stage: ProgressStage; elapsed_ms: number }
  | { event: "content"; segment_index: number; text: string; citations: string[] }
  | { event: "done"; message_id: number; validated: true }
  | { event: "failed"; reason: FailureReason; violations: string[] };
