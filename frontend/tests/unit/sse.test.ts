import { describe, expect, it } from "vitest";
import { readSse, SseParser } from "../../src/lib/sse";

describe("SseParser", () => {
  it("returns a frame once its blank line arrives, across pieces of any size", () => {
    const parser = new SseParser();
    expect(parser.push("event: prog")).toEqual([]);
    expect(parser.push('ress\ndata: {"stage": "retrieving"}\n')).toEqual([]);
    expect(parser.push("\nevent: done\ndata: {}\n\n")).toEqual([
      { event: "progress", data: '{"stage": "retrieving"}', id: null },
      { event: "done", data: "{}", id: null },
    ]);
  });

  it("joins data lines, ignores comments and takes CRLF and CR line ends", () => {
    const parser = new SseParser();
    expect(parser.push(": keepalive\r\nid: 7\r\ndata: a\rdata:b\r")).toEqual([]);
    // The CR at the end of the previous piece and this LF are one line end.
    expect(parser.push("\n\r\n")).toEqual([{ event: "message", data: "a\nb", id: "7" }]);
  });

  it("drops a frame without data and keeps an unfinished frame back", () => {
    const parser = new SseParser();
    expect(parser.push("event: lonely\n\nevent: content\ndata: half")).toEqual([]);
    expect(parser.push("")).toEqual([]);
  });
});

describe("readSse", () => {
  it("decodes a body split inside a multi-byte character", async () => {
    const bytes = new TextEncoder().encode('event: content\ndata: {"text": "提交"}\n\n');
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, 31));
        controller.enqueue(bytes.slice(31));
        controller.close();
      },
    });
    const frames = [];
    for await (const frame of readSse(body)) frames.push(frame);
    expect(frames).toEqual([{ event: "content", data: '{"text": "提交"}', id: null }]);
  });
});
