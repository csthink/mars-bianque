// Server-sent events framing (the HTML standard's event stream format), for streams
// that EventSource cannot read: the follow-up answer is the response of a POST.

export interface SseFrame {
  event: string;
  data: string;
  id: string | null;
}

/**
 * An incremental parser: feed it decoded text in pieces of any size and it returns the
 * frames completed so far. Lines end with LF, CR or CRLF; a blank line ends a frame;
 * lines starting with a colon are comments. A frame without data is not dispatched, and
 * text after the last blank line is an unfinished frame that the stream never completed.
 */
export class SseParser {
  private buffer = "";
  private event = "";
  private data: string[] = [];
  private id: string | null = null;

  push(text: string): SseFrame[] {
    this.buffer += text;
    const frames: SseFrame[] = [];
    let start = 0;
    for (let i = 0; i < this.buffer.length; i++) {
      const char = this.buffer[i];
      if (char !== "\n" && char !== "\r") continue;
      // A CR at the end of the buffer may be the first half of a CRLF: wait for more.
      if (char === "\r" && i + 1 === this.buffer.length) break;
      this.line(this.buffer.slice(start, i), frames);
      if (char === "\r" && this.buffer[i + 1] === "\n") i++;
      start = i + 1;
    }
    this.buffer = this.buffer.slice(start);
    return frames;
  }

  private line(line: string, frames: SseFrame[]) {
    if (line === "") {
      if (this.data.length > 0) frames.push({ event: this.event || "message", data: this.data.join("\n"), id: this.id });
      this.event = "";
      this.data = [];
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data.push(value);
    else if (field === "id") this.id = value;
  }
}

/** The frames of a response body, as they arrive. */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const parser = new SseParser();
  const decoder = new TextDecoder();
  const reader = body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      yield* parser.push(decoder.decode(value, { stream: true }));
    }
  } finally {
    // Stops the download when the reader leaves early; a no-op after the end.
    reader.cancel().catch(() => undefined);
  }
}
