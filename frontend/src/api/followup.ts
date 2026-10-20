import { readSse } from "../lib/sse";
import { errorOf } from "./client";
import type { FollowupEvent } from "./types";

const FOLLOWUP_EVENTS = new Set(["progress", "content", "done", "failed"]);

/** Gives the browser a turn between events, so that each one is painted on its own. */
const nextTask = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

/**
 * Ask a question and pass each event of the answer stream to ``onEvent``.
 *
 * The question is sent once. Nothing here retries: sending it again would add a turn to
 * the session, so a broken stream ends with an error and a person decides whether to
 * ask again. Rejects with a ``RequestError`` when the answer is not a
 * stream (401, 404, 409 ``session_busy``, 422 with the question to ask instead).
 */
export async function askQuestion(
  sessionId: number,
  question: string,
  onEvent: (event: FollowupEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const path = `/api/sessions/${sessionId}/messages`;
  const response = await fetch(path, {
    method: "POST",
    headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });
  if (!response.ok || response.body === null) throw await errorOf(path, response);
  for await (const frame of readSse(response.body)) {
    if (!FOLLOWUP_EVENTS.has(frame.event)) continue;
    onEvent({ event: frame.event, ...JSON.parse(frame.data) } as FollowupEvent);
    await nextTask();
  }
}
