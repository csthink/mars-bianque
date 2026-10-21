import { Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/base.css";
import { useEffect, useMemo, useRef, useState } from "react";
import type { PlanView, StepView } from "../api/types";
import { agentColor } from "../lib/agents";
import { flowLayout, LANE_HEIGHT, NODE_HEIGHT, NODE_WIDTH } from "../lib/flow";

/** The graph is never drawn smaller than this; a narrower screen scrolls sideways. */
const MIN_ZOOM = 0.85;

/** Room under the lanes for the library's attribution, so that it covers no step. */
const FOOTER_HEIGHT = 16;

const PENDING = {
  label: "等待",
  node: "border-slate-300 bg-white dark:border-slate-600 dark:bg-slate-900",
  dot: "bg-slate-400",
};

const STATUS: Record<string, { label: string; node: string; dot: string }> = {
  pending: PENDING,
  running: {
    label: "进行中",
    node: "border-blue-500 bg-blue-50 dark:border-blue-400 dark:bg-blue-950",
    dot: "bg-blue-500 animate-pulse",
  },
  done: {
    label: "完成",
    node: "border-emerald-500 bg-emerald-50 dark:border-emerald-500 dark:bg-emerald-950/70",
    dot: "bg-emerald-500",
  },
  failed: {
    label: "失败",
    node: "border-rose-500 bg-rose-50 dark:border-rose-400 dark:bg-rose-950",
    dot: "bg-rose-500",
  },
};

const statusOf = (status: string) => STATUS[status] ?? { ...PENDING, label: status };

export type StepNode = Node<{ step: StepView; version: number; onSelect: (step: StepView, version: number) => void }, "step">;
type LaneNode = Node<{ agent: string }, "lane">;
type ColumnNode = Node<{ version: number; source: string }, "column">;

/**
 * One plan step. The header holds the step number and the status on one line; a step of a
 * supplementary plan has its number in violet, under the column that names its round.
 */
export function StepCard({ data: { step, version, onSelect } }: NodeProps<StepNode>) {
  const status = statusOf(step.status);
  return (
    <div
      data-testid="flow-step"
      data-status={step.status}
      className={`h-full w-full rounded-lg border-2 shadow-sm ${status.node}`}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} className="!border-0 !bg-transparent" />
      <button
        type="button"
        title={step.goal}
        onClick={() => onSelect(step, version)}
        className="nodrag nopan flex h-full w-full cursor-pointer flex-col justify-start rounded-md px-2.5 py-1.5 text-left focus-visible:outline-2 focus-visible:outline-blue-500"
      >
        <span className="flex items-center justify-between gap-2 text-[11px] leading-4">
          <span
            title={version > 1 ? `第 ${version} 轮（补充计划）` : undefined}
            className={`whitespace-nowrap font-mono ${version > 1 ? "font-semibold text-violet-700 dark:text-violet-300" : "text-slate-500 dark:text-slate-400"}`}
          >
            v{version} · {step.seq}
          </span>
          <span className="flex items-center gap-1 whitespace-nowrap font-medium text-slate-700 dark:text-slate-200">
            <span aria-hidden className={`size-2 rounded-full ${status.dot}`} />
            {status.label}
          </span>
        </span>
        <span className="line-clamp-3 text-xs leading-4 text-slate-800 dark:text-slate-100">{step.goal}</span>
      </button>
      <Handle type="source" position={Position.Right} isConnectable={false} className="!border-0 !bg-transparent" />
    </div>
  );
}

function LaneBand({ data: { agent } }: NodeProps<LaneNode>) {
  const color = agentColor(agent);
  return (
    <div className={`flex h-full w-full items-center rounded-md ${color.lane}`}>
      <div className="w-[116px] shrink-0 px-3">
        <p className={`font-mono text-sm font-semibold ${color.text}`}>{agent}</p>
        <p className="text-[11px] text-slate-500 dark:text-slate-400">取证子 Agent</p>
      </div>
    </div>
  );
}

function ColumnHeader({ data: { version, source } }: NodeProps<ColumnNode>) {
  return (
    <div className="h-full w-full border-l border-dashed border-slate-300 dark:border-slate-600">
      <p className="truncate px-3.5 pt-1.5 text-xs font-medium text-slate-600 dark:text-slate-300">
        第 {version} 轮 · 计划 v{version}
        {version > 1 && "（补充）"}
        {source === "default" && "（默认计划）"}
      </p>
    </div>
  );
}

const nodeTypes = { step: StepCard, lane: LaneBand, column: ColumnHeader };

const fixed = { draggable: false, selectable: false, focusable: false } as const;

/** The container's width, followed as it changes. */
function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (element === null) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry?.contentRect.width ?? 0));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/**
 * The plan steps in lanes per sub-agent and columns per round. The graph is only
 * looked at: nothing moves or connects, and the wheel scrolls the page. Clicking a step
 * selects it in the plan panel.
 */
export function FlowGraph({
  plans,
  onSelect,
}: {
  plans: PlanView[];
  onSelect: (step: StepView, version: number) => void;
}) {
  const layout = useMemo(() => flowLayout(plans), [plans]);
  const [ref, available] = useWidth();
  const zoom = available === 0 ? 1 : Math.min(1, Math.max(MIN_ZOOM, available / layout.width));
  // The lanes reach across the whole container when the steps need less room.
  const width = Math.max(layout.width, available / zoom);

  const nodes = useMemo<Node[]>(
    () => [
      ...layout.lanes.map(
        (lane): LaneNode => ({
          id: `lane-${lane.agent}`,
          type: "lane",
          position: { x: 0, y: lane.y + 3 },
          width,
          height: LANE_HEIGHT - 6,
          data: { agent: lane.agent },
          zIndex: 0,
          ...fixed,
        }),
      ),
      ...layout.columns.map(
        (column): ColumnNode => ({
          id: `column-${column.version}`,
          type: "column",
          position: { x: column.x, y: 0 },
          width: column.width,
          height: layout.height,
          data: { version: column.version, source: column.source },
          zIndex: 1,
          ...fixed,
        }),
      ),
      ...layout.steps.map(
        ({ step, version, x, y }): StepNode => ({
          id: String(step.id),
          type: "step",
          position: { x, y },
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
          data: { step, version, onSelect },
          zIndex: 2,
          // The library gives nodes that can neither be selected nor dragged no pointer
          // events, so a click would land on the pane below; a step takes its clicks.
          style: { pointerEvents: "all" },
          ...fixed,
        }),
      ),
    ],
    [layout, width, onSelect],
  );
  const edges = useMemo<Edge[]>(() => {
    const status = new Map(layout.steps.map(({ step }) => [step.id, step.status]));
    return layout.edges.map((edge) => ({
      id: edge.id,
      source: String(edge.source),
      target: String(edge.target),
      animated: status.get(edge.target) === "running",
      zIndex: 2,
      focusable: false,
    }));
  }, [layout]);

  return (
    <div ref={ref} data-testid="flow-graph" className="bq-flow overflow-x-auto overflow-y-hidden">
      <div style={{ width: width * zoom, height: layout.height * zoom + FOOTER_HEIGHT }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          viewport={{ x: 0, y: 0, zoom }}
          minZoom={MIN_ZOOM}
          maxZoom={1}
          nodesDraggable={false}
          nodesConnectable={false}
          nodesFocusable={false}
          edgesFocusable={false}
          elementsSelectable={false}
          panOnDrag={false}
          panOnScroll={false}
          zoomOnScroll={false}
          zoomOnPinch={false}
          zoomOnDoubleClick={false}
          preventScrolling={false}
          aria-label="流程图：计划步骤按子 Agent 分泳道"
        />
      </div>
    </div>
  );
}
