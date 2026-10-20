import type { PlanView, StepView } from "../api/types";

/** The evidence sub-agents, one lane each, in this order. */
export const LANES = ["kubernetes", "logs", "change"] as const;

// Sizes in pixels at zoom 1.
export const LANE_LABEL_WIDTH = 116;
export const HEADER_HEIGHT = 34;
export const LANE_HEIGHT = 98;
export const NODE_WIDTH = 148;
export const NODE_HEIGHT = 78;
const NODE_GAP = 14;
const COLUMN_PADDING = 14;

export interface FlowLane {
  agent: string;
  y: number;
}

/** One column per plan version: the round in which its steps were dispatched. */
export interface FlowColumn {
  version: number;
  source: string;
  x: number;
  width: number;
}

export interface FlowStep {
  step: StepView;
  version: number;
  x: number;
  y: number;
}

export interface FlowEdge {
  id: string;
  source: number;
  target: number;
}

export interface FlowLayout {
  lanes: FlowLane[];
  columns: FlowColumn[];
  steps: FlowStep[];
  edges: FlowEdge[];
  width: number;
  height: number;
}

/**
 * Place every step of every plan version: the lane of its sub-agent, the column of its
 * version, left to right by ``seq`` within the cell. A supplementary plan holds only the
 * steps it adds, so there is one node per step. Consecutive steps of a cell are joined
 * in the order a sub-agent runs them. A domain outside ``LANES`` gets a lane of its own
 * below them, so no step is ever left out.
 */
export function flowLayout(plans: PlanView[]): FlowLayout {
  const ordered = [...plans].sort((a, b) => a.version - b.version);
  const agents: string[] = [...LANES];
  for (const step of ordered.flatMap((plan) => plan.steps)) {
    if (!agents.includes(step.domain)) agents.push(step.domain);
  }
  const lanes = agents.map((agent, index) => ({ agent, y: HEADER_HEIGHT + index * LANE_HEIGHT }));
  const laneY = new Map(lanes.map((lane) => [lane.agent, lane.y]));

  const columns: FlowColumn[] = [];
  const steps: FlowStep[] = [];
  const edges: FlowEdge[] = [];
  let x = LANE_LABEL_WIDTH;
  for (const plan of ordered) {
    const cells = agents.map((agent) =>
      plan.steps.filter((step) => step.domain === agent).sort((a, b) => a.seq - b.seq),
    );
    const widest = Math.max(1, ...cells.map((cell) => cell.length));
    const width = 2 * COLUMN_PADDING + widest * NODE_WIDTH + (widest - 1) * NODE_GAP;
    columns.push({ version: plan.version, source: plan.source, x, width });
    for (const cell of cells) {
      cell.forEach((step, index) => {
        steps.push({
          step,
          version: plan.version,
          x: x + COLUMN_PADDING + index * (NODE_WIDTH + NODE_GAP),
          y: (laneY.get(step.domain) ?? 0) + (LANE_HEIGHT - NODE_HEIGHT) / 2,
        });
        const previous = cell[index - 1];
        if (previous) edges.push({ id: `${previous.id}-${step.id}`, source: previous.id, target: step.id });
      });
    }
    x += width;
  }
  if (columns.length === 0) x += NODE_WIDTH + 2 * COLUMN_PADDING;
  return { lanes, columns, steps, edges, width: x, height: HEADER_HEIGHT + lanes.length * LANE_HEIGHT };
}
