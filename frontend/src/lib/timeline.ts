import type { CallView } from "../api/types";

/** The rows of the call timeline: the investigation agent first, then the domains. */
const AGENT_ORDER = ["investigation", "kubernetes", "logs", "change"];

export interface TimelineBar {
  call: CallView;
  /** Seconds after the investigation started. */
  start: number;
  end: number;
}

export interface TimelineRow {
  agent: string;
  bars: TimelineBar[];
}

export interface Timeline {
  rows: TimelineRow[];
  /** The right end of the axis in seconds, a multiple of the tick step. */
  axisEnd: number;
  ticks: number[];
}

export function buildTimeline(calls: CallView[], startedAt: string, elapsedMs: number): Timeline {
  const origin = new Date(startedAt).getTime();
  const rows = new Map<string, TimelineBar[]>();
  for (const call of calls) {
    const start = (new Date(call.started_at).getTime() - origin) / 1000;
    const bars = rows.get(call.agent) ?? [];
    bars.push({ call, start, end: start + (call.latency_ms ?? 0) / 1000 });
    rows.set(call.agent, bars);
  }
  const rank = (agent: string) => {
    const index = AGENT_ORDER.indexOf(agent);
    return index === -1 ? AGENT_ORDER.length : index;
  };
  const agents = [...rows.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  const last = Math.max(elapsedMs / 1000, ...[...rows.values()].flat().map((bar) => bar.end), 1);
  const step = last <= 30 ? 5 : last <= 120 ? 20 : last <= 600 ? 60 : 300;
  const axisEnd = Math.ceil(last / step) * step;
  const ticks = Array.from({ length: axisEnd / step + 1 }, (_, i) => i * step);
  return { rows: agents.map((agent) => ({ agent, bars: rows.get(agent) ?? [] })), axisEnd, ticks };
}
