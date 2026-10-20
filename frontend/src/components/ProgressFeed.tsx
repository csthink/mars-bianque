import { useState } from "react";
import type { StreamState } from "../api/events";
import type { InvestigationEvent } from "../api/types";
import { eventLabel, type StepIndex } from "../lib/events";
import { formatClock } from "../lib/format";

/** Events shown before the list is opened. */
export const RECENT_EVENTS = 6;

const STATE: Record<StreamState, { label: string; dot: string }> = {
  connecting: { label: "正在连接", dot: "bg-slate-400" },
  open: { label: "实时 · 已连接", dot: "bg-emerald-500 animate-pulse" },
  reconnecting: { label: "连接中断，正在重连", dot: "bg-amber-500 animate-pulse" },
  closed: { label: "已结束", dot: "bg-slate-400" },
};

/** The progress events of the investigation as they arrive. */
export function ProgressFeed({
  events,
  state,
  steps,
}: {
  events: InvestigationEvent[];
  state: StreamState;
  steps: StepIndex;
}) {
  const [open, setOpen] = useState(false);
  const shown = open ? events : events.slice(-RECENT_EVENTS);
  return (
    <div data-testid="progress-feed" data-state={state} className="mt-4 border-t border-slate-100 pt-3 dark:border-slate-800">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500 dark:text-slate-400">
        <p className="flex items-center gap-1.5 font-medium text-slate-700 dark:text-slate-200">
          <span aria-hidden className={`size-2 rounded-full ${STATE[state].dot}`} />
          进度事件 · <span data-testid="stream-state">{STATE[state].label}</span>
        </p>
        {events.length > RECENT_EVENTS && (
          <button
            type="button"
            onClick={() => setOpen(!open)}
            aria-expanded={open}
            className="rounded px-1 text-blue-700 hover:underline focus-visible:outline-2 focus-visible:outline-blue-500 dark:text-blue-300"
          >
            {open ? "只看最近" : `展开全部 ${events.length} 条`}
          </button>
        )}
      </div>
      {events.length === 0 ? (
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">还没有事件。</p>
      ) : (
        <ol className="mt-2 space-y-1 text-xs">
          {shown.map((event) => (
            <li
              key={event.seq}
              data-testid="progress-event"
              data-type={event.type}
              className="flex min-w-0 gap-2 text-slate-600 dark:text-slate-300"
            >
              <span className="shrink-0 text-slate-400 tabular-nums dark:text-slate-500">{formatClock(event.created_at)}</span>
              <span className="min-w-0 [overflow-wrap:anywhere]">{eventLabel(event, steps)}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
