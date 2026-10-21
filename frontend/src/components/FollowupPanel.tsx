import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { flushSync } from "react-dom";
import {
  ConflictError,
  createSession,
  RequestError,
  UnprocessableError,
  useFollowupSession,
  useQuestions,
  useSession,
  useSessions,
  useUnauthorizedHandler,
} from "../api/client";
import { askQuestion } from "../api/followup";
import type { FailureReason, FollowupMessage, ProgressStage, QuestionList } from "../api/types";
import { formatClock, formatDuration } from "../lib/format";
import { Citations } from "./Citation";
import { LoginPrompt } from "./LoginPrompt";
import { Badge, Muted, Section } from "./ui";

/** The longest question, as the backend accepts it. */
export const MAX_QUESTION_CHARS = 2000;

const STAGES: { stage: ProgressStage; label: string }[] = [
  { stage: "retrieving", label: "检索" },
  { stage: "generating", label: "生成" },
  { stage: "validating", label: "校验" },
];

const FAILURE: Record<FailureReason, string> = {
  gate_rejected: "回答未通过证据门，没有发布。",
  model_unavailable: "模型服务不可用。",
  model_error: "模型响应无法使用（回放缺少录制或配置错误）。",
  budget_exhausted: "本会话的 token 已用完，请新开会话。",
};

/** A question being answered: until it is stored, the page shows what the stream sent. */
export interface PendingTurn {
  question: string;
  stage: ProgressStage | null;
  elapsedMs: number;
  segments: { text: string; citations: string[] }[];
  outcome: "streaming" | "failed" | "broken";
  failure?: { reason: FailureReason; violations: string[] };
}

type Reference = { knowledge_id: string; title: string };

/**
 * Follow-up questions about the report. The answer arrives only after it has
 * passed the evidence gate: until then the stream carries progress stages, and a
 * rejected answer arrives as a reason without text. The sessions of the investigation
 * are kept by the backend; the latest one opens by default.
 */
export function FollowupPanel({
  investigationId,
  hasReport,
  suggested,
  references,
}: {
  investigationId: string;
  hasReport: boolean;
  /** The report's own follow-up questions, offered besides the case's question set. */
  suggested: string[];
  references: Reference[];
}) {
  const queryClient = useQueryClient();
  const session = useSession();
  const sessions = useSessions(investigationId);
  const questions = useQuestions(investigationId);
  const onUnauthorized = useUnauthorizedHandler();
  // null: the latest session; "new": a session is created with the next question.
  const [chosen, setChosen] = useState<number | "new" | null>(null);
  const activeId = chosen === "new" ? null : (chosen ?? sessions.data?.[0]?.id ?? null);
  const history = useFollowupSession(activeId);
  const [turn, setTurn] = useState<PendingTurn | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);

  const messages = activeId === null ? [] : (history.data?.messages ?? []);
  const asked = messages.filter((message) => message.role === "user").length;
  // Until the history is read, the next recorded question is not known either.
  const busy = turn?.outcome === "streaming" || (activeId !== null && history.isPending);

  const ask = async (question: string) => {
    setNotice(null);
    const controller = new AbortController();
    abort.current = controller;
    let sessionId = activeId;
    // Set by the event callback; declared so that the checks after the stream see it.
    let outcome = null as "done" | "failed" | null;
    let streaming = false;
    try {
      if (sessionId === null) {
        sessionId = (await createSession(investigationId)).id;
        setChosen(sessionId);
        void queryClient.invalidateQueries({ queryKey: ["followup-sessions", investigationId] });
      }
      setTurn({ question, stage: null, elapsedMs: 0, segments: [], outcome: "streaming" });
      streaming = true;
      await askQuestion(
        sessionId,
        question,
        // Each event is committed on its own, so every stage shows even when the
        // answer arrives within milliseconds of its last progress event.
        (event) => flushSync(() => {
          if (event.event === "progress") {
            setTurn((current) => current && { ...current, stage: event.stage, elapsedMs: event.elapsed_ms });
          } else if (event.event === "content") {
            setTurn(
              (current) =>
                current && { ...current, segments: [...current.segments, { text: event.text, citations: event.citations }] },
            );
          } else if (event.event === "failed") {
            outcome = "failed";
            setTurn((current) => current && { ...current, outcome: "failed", failure: event });
          } else {
            outcome = "done";
          }
        }),
        controller.signal,
      );
      if (outcome === null) {
        setTurn((current) => current && { ...current, outcome: "broken" });
      } else if (outcome === "done") {
        // The stored answer replaces what the stream showed once it has been read back.
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["followup-session", sessionId] }),
          queryClient.invalidateQueries({ queryKey: ["followup-sessions", investigationId] }),
        ]);
        setTurn(null);
      }
    } catch (failure) {
      if (controller.signal.aborted) return;
      if (onUnauthorized(failure)) {
        setTurn(null);
        return;
      }
      if (failure instanceof ConflictError) {
        setTurn(null);
        setNotice("本会话还有一个问题在回答，请稍后再问。");
      } else if (failure instanceof UnprocessableError) {
        setTurn(null);
        const expected = (failure.body as { expected?: string | null } | null)?.expected;
        setNotice(expected ? `回放形态下这一轮只能问：${expected}` : "回放形态下这个会话的录制问题已经问完，请新开会话。");
        void questions.refetch();
      } else if (failure instanceof RequestError || !streaming) {
        // The answer never started: nothing was asked, so it can simply be asked again.
        setTurn(null);
        setNotice(`提问失败：${(failure as Error).message}`);
      } else {
        setTurn((current) => current && { ...current, outcome: "broken" });
      }
    }
  };

  const sessionPicker = (sessions.data?.length ?? 0) > 0 && (
    <div className="flex flex-wrap items-center gap-2">
      <label className="sr-only" htmlFor="followup-session">
        追问会话
      </label>
      <select
        id="followup-session"
        value={chosen === "new" ? "new" : (activeId ?? "")}
        disabled={turn?.outcome === "streaming"}
        onChange={(event) => {
          setTurn(null);
          setNotice(null);
          setChosen(event.target.value === "new" ? "new" : Number(event.target.value));
        }}
        className="max-w-[16rem] rounded-md border border-slate-300 bg-white py-1 pr-7 pl-2 text-xs text-slate-700 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200"
      >
        {sessions.data?.map((item) => (
          <option key={item.id} value={item.id}>
            会话 #{item.id} · {item.turns} 轮 · {formatClock(item.created_at)}
          </option>
        ))}
        <option value="new">新会话（下一个问题时建立）</option>
      </select>
    </div>
  );

  return (
    <Section title="追问" aside={hasReport ? sessionPicker : undefined} testId="followup">
      {!hasReport ? (
        <Muted>调查有报告后才能追问。</Muted>
      ) : (
        <div className="space-y-4">
          {messages.length === 0 && turn === null && (
            <Muted>
              {activeId === null ? "围绕报告提问。回答同样要引用证据，校验通过后才会显示。" : "这个会话还没有问题。"}
            </Muted>
          )}
          {messages.length > 0 && (
            <ol className="space-y-4" aria-label="会话记录">
              {messages.map((message) => (
                <li key={message.id}>
                  {message.role === "user" ? (
                    <Question text={message.content} />
                  ) : (
                    <StoredAnswer message={message} references={references} />
                  )}
                </li>
              ))}
            </ol>
          )}
          {turn && <PendingAnswer turn={turn} />}
          {notice && (
            <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
              {notice}
            </p>
          )}
          <div className="border-t border-slate-100 pt-4 dark:border-slate-800">
            {!session.data ? (
              <LoginPrompt action="追问" />
            ) : questions.data?.recorded_only ? (
              <RecordedQuestions questions={questions.data} asked={asked} busy={busy} onAsk={(q) => void ask(q)} />
            ) : (
              <QuestionForm
                suggestions={[...new Set([...(questions.data?.questions.map((q) => q.question) ?? []), ...suggested])]}
                busy={busy}
                answering={turn?.outcome === "streaming"}
                onAsk={(q) => void ask(q)}
              />
            )}
          </div>
        </div>
      )}
    </Section>
  );
}

function Question({ text }: { text: string }) {
  return (
    <p
      data-testid="followup-question"
      className="ml-auto w-fit max-w-[90%] rounded-lg rounded-br-sm bg-slate-800 px-3 py-2 text-sm [overflow-wrap:anywhere] text-white dark:bg-slate-700"
    >
      {text}
    </p>
  );
}

function StoredAnswer({ message, references }: { message: FollowupMessage; references: Reference[] }) {
  const titles = new Map(references.map((item) => [item.knowledge_id, item.title]));
  return (
    <div data-testid="followup-answer" className="mt-2 space-y-2 text-sm text-slate-800 dark:text-slate-200">
      {message.segments.map((segment, index) => (
        <p key={index} data-testid="answer-segment" className="leading-relaxed [overflow-wrap:anywhere]">
          {segment.insufficient_evidence && (
            <span className="mr-1.5 align-middle">
              <Badge tone="warning">证据不足</Badge>
            </span>
          )}
          {segment.text} <Citations ids={segment.citations} />
        </p>
      ))}
      {message.references.length > 0 && (
        <p className="flex flex-wrap items-baseline gap-x-1.5 text-xs text-slate-500 dark:text-slate-400">
          <span>参考资料（知识，不作为证据）：</span>
          {message.references.map((id) => (
            <span key={id}>
              <span className="font-mono">{id}</span>
              {titles.has(id) && ` ${titles.get(id)}`}
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

/** What the stream has sent so far: stages only, then the paragraphs that passed. */
export function PendingAnswer({ turn }: { turn: PendingTurn }) {
  const reached = turn.stage === null ? -1 : STAGES.findIndex((item) => item.stage === turn.stage);
  const released = turn.segments.length > 0;
  return (
    <div className="space-y-2">
      <Question text={turn.question} />
      <div
        data-testid="pending-answer"
        data-stage={turn.stage ?? ""}
        data-outcome={turn.outcome}
        data-segments={turn.segments.length}
        className="space-y-2 text-sm text-slate-800 dark:text-slate-200"
      >
        {turn.outcome !== "failed" && (
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
            {STAGES.map((item, index) => {
              const done = released || index < reached;
              const current = !released && index === reached;
              return (
                <span
                  key={item.stage}
                  className={current ? "font-medium text-blue-700 dark:text-blue-300" : done ? "text-emerald-700 dark:text-emerald-400" : ""}
                >
                  {item.label}
                  {done ? " ✓" : current ? "中…" : ""}
                </span>
              );
            })}
            {turn.stage === "retrying" && !released && <span className="font-medium text-amber-700 dark:text-amber-300">重试中…</span>}
            {!released && turn.outcome === "streaming" && (
              <span className="tabular-nums">已用 {formatDuration(turn.elapsedMs)} · 校验通过后才释放正文</span>
            )}
          </p>
        )}
        {turn.segments.map((segment, index) => (
          <p key={index} data-testid="answer-segment" className="leading-relaxed [overflow-wrap:anywhere]">
            {segment.text} <Citations ids={segment.citations} />
          </p>
        ))}
        {turn.outcome === "failed" && turn.failure && (
          <p role="alert" data-testid="answer-failed" className="text-rose-700 dark:text-rose-300">
            {FAILURE[turn.failure.reason]}
            {turn.failure.violations.length > 0 && `（${turn.failure.violations.join("、")}）`}
          </p>
        )}
        {turn.outcome === "broken" && (
          <p role="alert" className="text-amber-800 dark:text-amber-300">
            连接中断，回答没有收完。问题没有自动重发，需要时请再问一次。
          </p>
        )}
      </div>
    </div>
  );
}

function RecordedQuestions({
  questions,
  asked,
  busy,
  onAsk,
}: {
  questions: QuestionList;
  asked: number;
  busy: boolean;
  onAsk: (question: string) => void;
}) {
  if (questions.questions.length === 0) return <Muted>这个用例没有录制问题。</Muted>;
  return (
    <div data-testid="recorded-questions" className="space-y-2">
      <p className="text-xs text-slate-500 dark:text-slate-400">回放形态：只能按录制顺序提问。</p>
      <ol className="space-y-1.5">
        {questions.questions.map((item, index) => (
          <li key={item.id}>
            <button
              type="button"
              disabled={busy || index !== asked}
              onClick={() => onAsk(item.question)}
              className="flex w-full items-start gap-2 rounded-md px-2.5 py-1.5 text-left text-sm text-slate-800 ring-1 ring-slate-200 ring-inset enabled:hover:bg-blue-50 enabled:hover:ring-blue-300 disabled:text-slate-400 dark:text-slate-200 dark:ring-slate-700 dark:enabled:hover:bg-blue-950 dark:disabled:text-slate-500"
            >
              <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{item.question}</span>
              {index < asked && <Badge tone="success">已问</Badge>}
            </button>
          </li>
        ))}
      </ol>
      {asked >= questions.questions.length && (
        <p className="text-xs text-slate-500 dark:text-slate-400">录制问题已经问完；要再问一遍请选「新会话」。</p>
      )}
    </div>
  );
}

function QuestionForm({
  suggestions,
  busy,
  answering,
  onAsk,
}: {
  suggestions: string[];
  busy: boolean;
  answering: boolean;
  onAsk: (question: string) => void;
}) {
  const [text, setText] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const question = text.trim();
    if (question === "" || busy) return;
    setText("");
    onAsk(question);
  };
  return (
    <form onSubmit={submit} className="space-y-2">
      {suggestions.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {suggestions.map((question) => (
            <button
              key={question}
              type="button"
              onClick={() => setText(question)}
              className="max-w-full rounded-full px-2.5 py-1 text-left text-xs text-slate-600 ring-1 ring-slate-300 ring-inset [overflow-wrap:anywhere] hover:bg-slate-100 dark:text-slate-300 dark:ring-slate-600 dark:hover:bg-slate-800"
            >
              {question}
            </button>
          ))}
        </div>
      )}
      <label className="sr-only" htmlFor="followup-question">
        问题
      </label>
      <textarea
        id="followup-question"
        rows={2}
        maxLength={MAX_QUESTION_CHARS}
        value={text}
        placeholder="例如：为什么认定是这次提交？"
        onChange={(event) => setText(event.target.value)}
        className="block w-full resize-y rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-900 focus:border-blue-500 focus:outline-2 focus:outline-blue-500/40 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
      />
      <div className="flex justify-end">
        <button
          type="submit"
          disabled={busy || text.trim() === ""}
          className="rounded-md bg-slate-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
        >
          {answering ? "回答中…" : "提问"}
        </button>
      </div>
    </form>
  );
}
