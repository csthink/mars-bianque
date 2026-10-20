import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { INVESTIGATION_EVENT_TYPES, TERMINAL_EVENTS, type InvestigationEvent } from "./types";

/** Connection state of the progress event stream. */
export type StreamState = "connecting" | "open" | "reconnecting" | "closed";

/** Events within this many milliseconds lead to one read of the detail and the calls. */
export const REFRESH_DELAY_MS = 300;

const isTerminal = (type: string) => (TERMINAL_EVENTS as readonly string[]).includes(type);

/**
 * The progress events of an investigation, read from the first one.
 *
 * The stream ends after the terminal event and the page closes it then; otherwise the
 * browser would connect again. A broken connection is resumed by ``EventSource`` with
 * ``Last-Event-ID``. While ``refresh`` is true (the investigation was still open when
 * the page read it), each event makes the detail and the calls stale, so they are read
 * again; events of a finished investigation are history and change nothing.
 */
export function useInvestigationEvents(id: string, enabled: boolean, refresh: boolean) {
  const queryClient = useQueryClient();
  const [events, setEvents] = useState<InvestigationEvent[]>([]);
  const [state, setState] = useState<StreamState>("connecting");
  const refreshRef = useRef(refresh);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  useEffect(() => {
    if (!enabled) return;
    const source = new EventSource(`/api/investigations/${encodeURIComponent(id)}/events`);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const reload = () => {
      timer = undefined;
      void queryClient.invalidateQueries({ queryKey: ["investigation", id] });
      void queryClient.invalidateQueries({ queryKey: ["calls", id] });
    };
    const onEvent = (message: MessageEvent<string>) => {
      const event = JSON.parse(message.data) as InvestigationEvent;
      setEvents((previous) => (previous.some((item) => item.seq === event.seq) ? previous : [...previous, event]));
      if (isTerminal(event.type)) {
        source.close();
        setState("closed");
      }
      if (!refreshRef.current) return;
      if (isTerminal(event.type)) {
        clearTimeout(timer);
        reload();
      } else {
        timer ??= setTimeout(reload, REFRESH_DELAY_MS);
      }
    };
    for (const type of INVESTIGATION_EVENT_TYPES) source.addEventListener(type, onEvent);
    source.onopen = () => setState("open");
    source.onerror = () => setState(source.readyState === EventSource.CLOSED ? "closed" : "reconnecting");
    return () => {
      clearTimeout(timer);
      source.close();
    };
  }, [id, enabled, queryClient]);

  return { events, state };
}
