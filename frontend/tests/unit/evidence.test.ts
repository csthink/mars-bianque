import { describe, expect, it } from "vitest";
import { byCollection } from "../../src/lib/evidence";
import { evidence } from "./fixtures";

describe("byCollection", () => {
  it("orders by collection time, then by ID", () => {
    const ordered = byCollection([
      evidence({ id: "ev_b", collected_at: "2026-10-19T22:34:04Z" }),
      evidence({ id: "ev_c", collected_at: "2026-10-19T22:33:58Z", source: "alertmanager" }),
      evidence({ id: "ev_a", collected_at: "2026-10-19T22:34:04Z" }),
    ]);
    expect(ordered.map((item) => item.id)).toEqual(["ev_c", "ev_a", "ev_b"]);
  });
});
