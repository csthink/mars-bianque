import { describe, expect, it } from "vitest";
import { formatDuration, formatTokens } from "../../src/lib/format";

describe("formatTokens", () => {
  it("keeps small counts and abbreviates thousands", () => {
    expect(formatTokens(980)).toBe("980");
    expect(formatTokens(38210)).toBe("38.2k");
    expect(formatTokens(61805)).toBe("61.8k");
  });
});

describe("formatDuration", () => {
  it("uses seconds below a minute and minutes above", () => {
    expect(formatDuration(210)).toBe("0.21 秒");
    expect(formatDuration(5200)).toBe("5.2 秒");
    expect(formatDuration(5000)).toBe("5 秒");
    expect(formatDuration(84000)).toBe("1 分 24 秒");
  });
});
