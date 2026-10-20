import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { REFRESH_DELAY_MS, useInvestigationEvents } from "../../src/api/events";
import type { SuggestionView } from "../../src/api/types";
import { FollowupPanel, PendingAnswer } from "../../src/components/FollowupPanel";
import { SuggestionList } from "../../src/components/SuggestionList";
import { safeNext } from "../../src/lib/login";
import { LoginPage } from "../../src/pages/LoginPage";

type Route = (init: RequestInit | undefined) => Response | Promise<Response>;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answers requests by method and path; an unexpected request fails the test. */
function serve(routes: Record<string, Route>) {
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`;
    const route = routes[key];
    if (!route) throw new Error(`unexpected request ${key}`);
    return route(init);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function wrapper(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/investigations/inv_1"]}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const pending: SuggestionView = {
  id: "sg_1",
  seq: 1,
  action: "Revert the commit.",
  command: "git revert 9b9a6ea8a72b",
  risk: "change",
  citations: [],
  state: "pending",
  decided_by: null,
  decided_at: null,
  decision_comment: null,
};

const decided: SuggestionView = {
  ...pending,
  state: "confirmed",
  decided_by: "demo",
  decided_at: "2026-10-19T22:40:00Z",
  decision_comment: "演示确认",
};

const loggedIn = { "GET /api/auth/session": () => json(200, { username: "demo" }) };
const loggedOut = { "GET /api/auth/session": () => json(401, { detail: "login required" }) };

describe("SuggestionList", () => {
  it("asks for a login instead of showing the buttons", async () => {
    serve(loggedOut);
    render(<SuggestionList investigationId="inv_1" suggestions={[pending]} />, { wrapper: wrapper() });
    expect(await screen.findByTestId("login-prompt")).toHaveTextContent("登录后可确认或驳回");
    expect(screen.queryByRole("button", { name: "确认" })).toBeNull();
    expect(screen.getByRole("link", { name: "登录" })).toHaveAttribute(
      "href",
      "/login?next=%2Finvestigations%2Finv_1",
    );
  });

  it("confirms with a comment and records who decided", async () => {
    const fetch = serve({
      ...loggedIn,
      "POST /api/investigations/inv_1/suggestions/sg_1/decision": () => json(201, decided),
    });
    const client = new QueryClient();
    client.setQueryData(["investigation", "inv_1"], { suggestions: [pending] });
    const { rerender } = render(<SuggestionList investigationId="inv_1" suggestions={[pending]} />, {
      wrapper: wrapper(client),
    });
    await userEvent.click(await screen.findByRole("button", { name: "确认" }));
    await userEvent.type(screen.getByLabelText("确认说明（可留空）"), "演示确认");
    await userEvent.click(screen.getByRole("button", { name: "提交确认" }));
    await waitFor(() =>
      expect(client.getQueryData<{ suggestions: SuggestionView[] }>(["investigation", "inv_1"])?.suggestions).toEqual([
        decided,
      ]),
    );
    const call = fetch.mock.calls.find(([input]) => String(input).endsWith("/decision"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ decision: "confirmed", comment: "演示确认" });
    rerender(<SuggestionList investigationId="inv_1" suggestions={[decided]} />);
    expect(screen.getByTestId("decision")).toHaveTextContent("已确认 · 操作人 demo");
    expect(screen.getByTestId("decision")).toHaveTextContent("说明：演示确认");
    expect(screen.queryByRole("button", { name: "确认" })).toBeNull();
  });

  it("says so when someone else decided first", async () => {
    serve({
      ...loggedIn,
      "POST /api/investigations/inv_1/suggestions/sg_1/decision": () => json(409, { state: "rejected" }),
    });
    const { rerender } = render(<SuggestionList investigationId="inv_1" suggestions={[pending]} />, {
      wrapper: wrapper(),
    });
    await userEvent.click(await screen.findByRole("button", { name: "驳回" }));
    await userEvent.click(screen.getByRole("button", { name: "提交驳回" }));
    // The detail is read again and carries the other decision.
    const theirs = { ...decided, state: "rejected" as const, decided_by: "other", decision_comment: null };
    rerender(<SuggestionList investigationId="inv_1" suggestions={[theirs]} />);
    expect(await screen.findByRole("status")).toHaveTextContent("已由他人决定，未改动。");
    expect(screen.getByTestId("decision")).toHaveTextContent("已驳回 · 操作人 other");
  });
});

describe("PendingAnswer", () => {
  it("shows the stages and no text before the answer is released", () => {
    render(
      <PendingAnswer
        turn={{ question: "Why?", stage: "validating", elapsedMs: 2100, segments: [], outcome: "streaming" }}
      />,
    );
    const answer = screen.getByTestId("pending-answer");
    expect(answer).toHaveTextContent("检索 ✓生成 ✓校验中…");
    expect(answer).toHaveTextContent("已用 2.1 秒 · 校验通过后才释放正文");
    expect(screen.queryByTestId("answer-segment")).toBeNull();
  });

  it("shows a rejected answer as its reason and validators only", () => {
    render(
      <PendingAnswer
        turn={{
          question: "Why?",
          stage: "validating",
          elapsedMs: 0,
          segments: [],
          outcome: "failed",
          failure: { reason: "gate_rejected", violations: ["citation_integrity"] },
        }}
      />,
    );
    expect(screen.getByTestId("answer-failed")).toHaveTextContent("回答未通过证据门，没有发布。（citation_integrity）");
    expect(screen.getByTestId("pending-answer")).not.toHaveTextContent("校验");
  });
});

/** A response body that sends the given frames and ends. */
function stream(...frames: string[]) {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const frame of frames) controller.enqueue(encoder.encode(frame));
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

describe("FollowupPanel", () => {
  const recorded = {
    recorded_only: true,
    questions: [
      { id: "q1", question: "Why this commit?" },
      { id: "q2", question: "What else changed?" },
    ],
  };
  const session = { id: 5, investigation_id: "inv_1", created_by: "demo", created_at: "2026-10-19T22:40:00Z" };
  const turn = [
    { id: 1, seq: 1, role: "user", content: "Why this commit?", segments: [], citations: [], references: [], unknown: false, created_at: "" },
    {
      id: 2,
      seq: 2,
      role: "assistant",
      content: "Because.",
      segments: [{ text: "Because.", citations: [], insufficient_evidence: false }],
      citations: [],
      references: [],
      unknown: false,
      created_at: "",
    },
  ];

  it("offers only the next recorded question of the latest session", async () => {
    serve({
      ...loggedIn,
      "GET /api/investigations/inv_1/sessions": () => json(200, { sessions: [{ ...session, turns: 1 }] }),
      "GET /api/sessions/5": () => json(200, { session, messages: turn }),
      "GET /api/investigations/inv_1/questions": () => json(200, recorded),
    });
    render(<FollowupPanel investigationId="inv_1" hasReport suggested={[]} references={[]} />, { wrapper: wrapper() });
    expect(await screen.findByTestId("answer-segment")).toHaveTextContent("Because.");
    const first = screen.getByRole("button", { name: /Why this commit\?/ });
    expect(first).toBeDisabled();
    expect(first).toHaveTextContent("已问");
    expect(screen.getByRole("button", { name: "What else changed?" })).toBeEnabled();
  });

  it("creates a session with the first question and shows a rejection without text", async () => {
    const fetch = serve({
      ...loggedIn,
      "GET /api/investigations/inv_1/sessions": () => json(200, { sessions: [] }),
      "GET /api/investigations/inv_1/questions": () => json(200, { recorded_only: false, questions: [] }),
      "POST /api/investigations/inv_1/sessions": () => json(201, session),
      "GET /api/sessions/5": () => json(200, { session, messages: [] }),
      "POST /api/sessions/5/messages": () =>
        stream(
          'event: progress\ndata: {"stage": "generating", "elapsed_ms": 10}\n\n',
          'event: failed\ndata: {"reason": "gate_rejected", "violations": ["write_command"]}\n\n',
        ),
    });
    render(
      <FollowupPanel investigationId="inv_1" hasReport suggested={["Why this commit?"]} references={[]} />,
      { wrapper: wrapper() },
    );
    await userEvent.click(await screen.findByRole("button", { name: "Why this commit?" }));
    await userEvent.click(screen.getByRole("button", { name: "提问" }));
    expect(await screen.findByTestId("answer-failed")).toHaveTextContent("回答未通过证据门，没有发布。（write_command）");
    expect(screen.queryByTestId("answer-segment")).toBeNull();
    const asked = fetch.mock.calls.filter(([input]) => String(input) === "/api/sessions/5/messages");
    expect(asked).toHaveLength(1);
    expect(JSON.parse(String(asked[0]?.[1]?.body))).toEqual({ question: "Why this commit?" });
  });

  it("says the stream broke and does not ask again", async () => {
    const fetch = serve({
      ...loggedIn,
      "GET /api/investigations/inv_1/sessions": () => json(200, { sessions: [{ ...session, turns: 0 }] }),
      "GET /api/sessions/5": () => json(200, { session, messages: [] }),
      "GET /api/investigations/inv_1/questions": () => json(200, { recorded_only: false, questions: [] }),
      "POST /api/sessions/5/messages": () => stream('event: progress\ndata: {"stage": "retrieving", "elapsed_ms": 1}\n\n'),
    });
    render(<FollowupPanel investigationId="inv_1" hasReport suggested={[]} references={[]} />, { wrapper: wrapper() });
    await userEvent.type(await screen.findByLabelText("问题"), "Why?");
    await userEvent.click(screen.getByRole("button", { name: "提问" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("连接中断，回答没有收完。问题没有自动重发");
    expect(fetch.mock.calls.filter(([input]) => String(input) === "/api/sessions/5/messages")).toHaveLength(1);
  });
});

describe("login", () => {
  it("only goes on to a path of this site", () => {
    expect(safeNext("/investigations/inv_1")).toBe("/investigations/inv_1");
    for (const next of [null, "https://example.com", "//example.com", "/\\example.com", "investigations"]) {
      expect(safeNext(next)).toBe("/");
    }
  });

  it("tells a wrong password without telling which field", async () => {
    serve({ "POST /api/auth/login": () => json(401, { detail: "invalid username or password" }) });
    render(<LoginPage />, { wrapper: wrapper() });
    await userEvent.type(screen.getByLabelText("用户名"), "demo");
    await userEvent.type(screen.getByLabelText("口令"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("用户名或口令不对。");
  });
});

/** Stands in for the browser's EventSource. */
class FakeEventSource {
  static readonly CLOSED = 2;
  static last: FakeEventSource | null = null;
  readyState = 0;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, ((message: MessageEvent<string>) => void)[]>();

  constructor(readonly url: string) {
    FakeEventSource.last = this;
  }

  addEventListener(type: string, listener: (message: MessageEvent<string>) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close() {
    this.readyState = FakeEventSource.CLOSED;
  }

  emit(type: string, seq: number, fields: Record<string, unknown> = {}) {
    const data = JSON.stringify({ seq, type, created_at: "2026-10-19T22:34:00Z", ...fields });
    for (const listener of this.listeners.get(type) ?? []) listener(new MessageEvent(type, { data }));
  }
}

describe("useInvestigationEvents", () => {
  beforeEach(() => {
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  function setUp(refresh: boolean) {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries").mockResolvedValue();
    const hook = renderHook(() => useInvestigationEvents("inv_1", true, refresh), { wrapper: wrapper(client) });
    const source = FakeEventSource.last;
    if (source === null) throw new Error("no EventSource opened");
    return { hook, invalidate, source };
  }

  it("reads the detail and the calls once for events that come together", () => {
    vi.useFakeTimers();
    const { hook, invalidate, source } = setUp(true);
    expect(source.url).toBe("/api/investigations/inv_1/events");
    act(() => {
      source.emit("step_started", 1, { step_id: 1, domain: "logs", status: "running" });
      source.emit("evidence_added", 2, { evidence_id: "ev_1", source: "logs", tool: "logs_get" });
    });
    expect(hook.result.current.events.map((event) => event.seq)).toEqual([1, 2]);
    expect(invalidate).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(REFRESH_DELAY_MS));
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["investigation", "inv_1"],
      ["calls", "inv_1"],
    ]);
  });

  it("closes the stream on the terminal event, so the browser does not connect again", () => {
    const { hook, invalidate, source } = setUp(true);
    act(() => source.emit("investigation_finished", 9, { status: "reported" }));
    expect(source.readyState).toBe(FakeEventSource.CLOSED);
    expect(hook.result.current.state).toBe("closed");
    expect(invalidate).toHaveBeenCalledTimes(2);
  });

  it("reads nothing again for the events of a finished investigation", () => {
    vi.useFakeTimers();
    const { hook, invalidate, source } = setUp(false);
    act(() => {
      source.emit("step_finished", 1, { step_id: 1, domain: "logs", status: "done" });
      source.emit("step_finished", 1, { step_id: 1, domain: "logs", status: "done" });
      source.emit("investigation_finished", 2, { status: "reported" });
      vi.advanceTimersByTime(REFRESH_DELAY_MS);
    });
    expect(hook.result.current.events.map((event) => event.seq)).toEqual([1, 2]);
    expect(invalidate).not.toHaveBeenCalled();
  });
});
