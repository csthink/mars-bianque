import type { Tone } from "../components/ui";

/** Colors of the agents: the investigation agent and the three evidence domains. */
export const AGENT_COLORS: Record<string, { text: string; bar: string; lane: string; chip: Tone }> = {
  investigation: {
    text: "text-slate-700 dark:text-slate-300",
    bar: "bg-slate-600 dark:bg-slate-400",
    lane: "bg-slate-100 dark:bg-slate-800/60",
    chip: "neutral",
  },
  kubernetes: {
    text: "text-blue-700 dark:text-blue-300",
    bar: "bg-blue-600 dark:bg-blue-400",
    lane: "bg-blue-50 dark:bg-blue-950/50",
    chip: "info",
  },
  logs: {
    text: "text-teal-700 dark:text-teal-300",
    bar: "bg-teal-600 dark:bg-teal-400",
    lane: "bg-teal-50 dark:bg-teal-950/50",
    chip: "success",
  },
  change: {
    text: "text-orange-700 dark:text-orange-300",
    bar: "bg-orange-600 dark:bg-orange-400",
    lane: "bg-orange-50 dark:bg-orange-950/50",
    chip: "warning",
  },
};

export const agentColor = (agent: string) =>
  AGENT_COLORS[agent] ?? (AGENT_COLORS.investigation as (typeof AGENT_COLORS)[string]);
