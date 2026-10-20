import { describe, expect, it } from "vitest";
import { buildTimeline } from "../../src/lib/timeline";
import { call } from "./fixtures";

describe("buildTimeline", () => {
  it("groups calls by agent, investigation agent first, domains in their order", () => {
    const timeline = buildTimeline(
      [
        call({ id: 1, agent: "change", started_at: "2026-10-19T22:34:40Z" }),
        call({ id: 2, agent: "logs", started_at: "2026-10-19T22:34:06Z" }),
        call({ id: 3, agent: "investigation", started_at: "2026-10-19T22:34:00Z" }),
        call({ id: 4, agent: "kubernetes", started_at: "2026-10-19T22:34:06Z" }),
      ],
      "2026-10-19T22:34:00Z",
      84000,
    );
    expect(timeline.rows.map((row) => row.agent)).toEqual(["investigation", "kubernetes", "logs", "change"]);
  });

  it("places bars in seconds after the start and rounds the axis up to a tick", () => {
    const timeline = buildTimeline(
      [call({ started_at: "2026-10-19T22:34:06.5Z", latency_ms: 1500 })],
      "2026-10-19T22:34:00Z",
      84000,
    );
    expect(timeline.rows[0]?.bars[0]).toMatchObject({ start: 6.5, end: 8 });
    expect(timeline.axisEnd).toBe(100);
    expect(timeline.ticks).toEqual([0, 20, 40, 60, 80, 100]);
  });

  it("stretches the axis to a call that ends after the recorded elapsed time", () => {
    const timeline = buildTimeline(
      [call({ started_at: "2026-10-19T22:34:20Z", latency_ms: 15000 })],
      "2026-10-19T22:34:00Z",
      10000,
    );
    expect(timeline.axisEnd).toBe(40);
  });
});
