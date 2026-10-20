import { describe, expect, it } from "vitest";
import { flowLayout, HEADER_HEIGHT, LANE_HEIGHT } from "../../src/lib/flow";
import { plan, step } from "./fixtures";

describe("flowLayout", () => {
  const plans = [
    plan(2, [step({ id: 7, seq: 1, domain: "change" }), step({ id: 8, seq: 2, domain: "change" })]),
    plan(1, [
      step({ id: 2, seq: 2, domain: "kubernetes" }),
      step({ id: 1, seq: 1, domain: "kubernetes" }),
      step({ id: 3, seq: 3, domain: "logs" }),
    ]),
  ];

  it("has one node per step of every version", () => {
    expect(flowLayout(plans).steps).toHaveLength(5);
  });

  it("puts each step in its sub-agent's lane and its version's column, in seq order", () => {
    const layout = flowLayout(plans);
    expect(layout.lanes.map((lane) => lane.agent)).toEqual(["kubernetes", "logs", "change"]);
    expect(layout.columns.map((column) => column.version)).toEqual([1, 2]);
    const at = (id: number) => {
      const found = layout.steps.find((item) => item.step.id === id);
      if (!found) throw new Error(`step ${id} not placed`);
      return found;
    };
    const second = layout.columns[1]?.x ?? Number.NaN;
    expect(at(1).x).toBeLessThan(at(2).x);
    expect(at(1).y).toBe(at(2).y);
    expect(at(3).y).toBeGreaterThan(at(1).y);
    expect(at(7).version).toBe(2);
    expect(at(7).x).toBeGreaterThanOrEqual(second);
    expect(at(3).x).toBeLessThan(second);
  });

  it("joins consecutive steps of a cell only", () => {
    expect(flowLayout(plans).edges.map((edge) => [edge.source, edge.target])).toEqual([
      [1, 2],
      [7, 8],
    ]);
  });

  it("draws the three lanes before any plan, and gives an unknown domain a lane", () => {
    const empty = flowLayout([]);
    expect(empty.lanes).toHaveLength(3);
    expect(empty.height).toBe(HEADER_HEIGHT + 3 * LANE_HEIGHT);
    const odd = flowLayout([plan(1, [step({ domain: "metrics" })])]);
    expect(odd.lanes.map((lane) => lane.agent)).toEqual(["kubernetes", "logs", "change", "metrics"]);
    expect(odd.steps).toHaveLength(1);
  });
});
