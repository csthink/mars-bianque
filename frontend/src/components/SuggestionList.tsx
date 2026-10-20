import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { ConflictError, decide, useSession, useUnauthorizedHandler } from "../api/client";
import type { InvestigationDetail, SuggestionView } from "../api/types";
import { formatDateTime } from "../lib/format";
import { Citations } from "./Citation";
import { LoginPrompt } from "./LoginPrompt";
import { Badge, Muted, Section } from "./ui";

const STATE = {
  pending: { label: "待确认", tone: "neutral" },
  confirmed: { label: "已确认", tone: "success" },
  rejected: { label: "已驳回", tone: "danger" },
} as const;

/** The longest decision comment, as the backend accepts it. */
export const MAX_COMMENT_CHARS = 2000;

type Decision = "confirmed" | "rejected";

const DECISION: Record<Decision, { verb: string; button: string }> = {
  confirmed: {
    verb: "确认",
    button: "bg-emerald-700 text-white hover:bg-emerald-600 dark:bg-emerald-600 dark:hover:bg-emerald-500",
  },
  rejected: {
    verb: "驳回",
    button: "bg-rose-700 text-white hover:bg-rose-600 dark:bg-rose-600 dark:hover:bg-rose-500",
  },
};

const secondary =
  "rounded-md px-3 py-1.5 text-sm font-medium text-slate-700 ring-1 ring-slate-300 ring-inset hover:bg-slate-100 disabled:opacity-50 dark:text-slate-200 dark:ring-slate-600 dark:hover:bg-slate-800";

const NO_COMMAND = <span className="text-xs text-slate-500 dark:text-slate-400">只记录决定，不执行命令</span>;

export function SuggestionList({
  investigationId,
  suggestions,
  highlighted = null,
}: {
  investigationId: string;
  suggestions: SuggestionView[];
  /** The suggestion a citation points at, from the URL hash. */
  highlighted?: string | null;
}) {
  const session = useSession();
  return (
    <Section title="修复建议" testId="suggestions">
      {suggestions.length === 0 ? (
        <Muted>没有修复建议。</Muted>
      ) : (
        <ul className="space-y-3">
          {suggestions.map((suggestion) => (
            <SuggestionItem
              key={suggestion.id}
              investigationId={investigationId}
              suggestion={suggestion}
              loggedIn={Boolean(session.data)}
              highlighted={suggestion.id === highlighted}
            />
          ))}
        </ul>
      )}
    </Section>
  );
}

function SuggestionItem({
  investigationId,
  suggestion,
  loggedIn,
  highlighted,
}: {
  investigationId: string;
  suggestion: SuggestionView;
  loggedIn: boolean;
  highlighted: boolean;
}) {
  // Set when someone else decided first: the decision shown is theirs, not this page's.
  const [conflict, setConflict] = useState(false);
  return (
    <li
      id={suggestion.id}
      data-testid="suggestion"
      data-state={suggestion.state}
      data-highlighted={highlighted ? "true" : undefined}
      className={`scroll-mt-4 rounded-lg border p-3 text-sm transition-colors ${
        highlighted
          ? "border-violet-400 bg-violet-50/60 ring-2 ring-violet-400 dark:border-violet-500 dark:bg-violet-950/40"
          : "border-slate-200 dark:border-slate-700"
      }`}
    >
      <p className="font-medium [overflow-wrap:anywhere] text-slate-900 dark:text-slate-100">{suggestion.action}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {suggestion.command && (
          <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs [overflow-wrap:anywhere] text-slate-700 dark:bg-slate-800 dark:text-slate-300">
            {suggestion.command}
          </code>
        )}
        <Badge tone={suggestion.risk === "change" ? "warning" : "success"}>
          {suggestion.risk === "change" ? "变更" : "只读"}
        </Badge>
        <Badge tone={STATE[suggestion.state].tone}>{STATE[suggestion.state].label}</Badge>
      </div>
      <div className="mt-2">
        <Citations ids={suggestion.citations} />
      </div>
      <div className="mt-3 border-t border-slate-100 pt-3 dark:border-slate-800">
        {suggestion.state !== "pending" ? (
          <DecisionRecord suggestion={suggestion} conflict={conflict} />
        ) : loggedIn ? (
          <DecisionControls
            investigationId={investigationId}
            suggestion={suggestion}
            onConflict={() => setConflict(true)}
          />
        ) : (
          <LoginPrompt action="确认或驳回" />
        )}
      </div>
    </li>
  );
}

function DecisionRecord({ suggestion, conflict }: { suggestion: SuggestionView; conflict: boolean }) {
  return (
    <div data-testid="decision" className="space-y-1 text-xs text-slate-500 dark:text-slate-400">
      {conflict && (
        <p role="status" className="font-medium text-amber-800 dark:text-amber-300">
          已由他人决定，未改动。
        </p>
      )}
      <p>
        {STATE[suggestion.state].label} · 操作人 {suggestion.decided_by}
        {suggestion.decided_at && ` · ${formatDateTime(suggestion.decided_at)}`}
      </p>
      {suggestion.decision_comment && (
        <p className="[overflow-wrap:anywhere] text-slate-600 dark:text-slate-300">说明：{suggestion.decision_comment}</p>
      )}
    </div>
  );
}

/** Confirm or reject, once. A decision is only recorded; no command runs. */
function DecisionControls({
  investigationId,
  suggestion,
  onConflict,
}: {
  investigationId: string;
  suggestion: SuggestionView;
  onConflict: () => void;
}) {
  const queryClient = useQueryClient();
  const onUnauthorized = useUnauthorizedHandler();
  const [decision, setDecision] = useState<Decision | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (decision === null) return;
    setBusy(true);
    setError(null);
    try {
      const decided = await decide(investigationId, suggestion.id, decision, comment);
      queryClient.setQueryData<InvestigationDetail>(
        ["investigation", investigationId],
        (detail) =>
          detail && {
            ...detail,
            suggestions: detail.suggestions.map((item) => (item.id === decided.id ? decided : item)),
          },
      );
    } catch (failure) {
      setBusy(false);
      if (onUnauthorized(failure)) return;
      if (failure instanceof ConflictError) {
        onConflict();
        void queryClient.invalidateQueries({ queryKey: ["investigation", investigationId] });
        return;
      }
      setError(`提交失败：${(failure as Error).message}`);
    }
  };

  if (decision === null) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setDecision("confirmed")}
          className={`rounded-md px-3 py-1.5 text-sm font-medium ${DECISION.confirmed.button}`}
        >
          确认
        </button>
        <button type="button" onClick={() => setDecision("rejected")} className={secondary}>
          驳回
        </button>
        {NO_COMMAND}
      </div>
    );
  }
  const { verb, button } = DECISION[decision];
  return (
    <form onSubmit={(event) => void submit(event)} className="space-y-2">
      <label className="block text-xs font-medium text-slate-600 dark:text-slate-300">
        {verb}说明（可留空）
        <textarea
          name="comment"
          rows={2}
          maxLength={MAX_COMMENT_CHARS}
          autoFocus
          value={comment}
          onChange={(event) => setComment(event.target.value)}
          className="mt-1 block w-full resize-y rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-900 focus:border-blue-500 focus:outline-2 focus:outline-blue-500/40 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
        />
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <button type="submit" disabled={busy} className={`rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${button}`}>
          {busy ? "正在提交…" : `提交${verb}`}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setDecision(null);
            setError(null);
          }}
          className={secondary}
        >
          取消
        </button>
        {NO_COMMAND}
      </div>
      {error && (
        <p role="alert" className="text-sm text-rose-700 dark:text-rose-300">
          {error}
        </p>
      )}
    </form>
  );
}
