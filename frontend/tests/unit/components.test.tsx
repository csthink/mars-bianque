import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { CallTable } from "../../src/components/CallTable";
import { CallTimeline } from "../../src/components/CallTimeline";
import { Citation } from "../../src/components/Citation";
import { EvidenceIds } from "../../src/components/evidenceContext";
import { callList } from "./fixtures";

function withEvidence(ids: string[], node: ReactNode) {
  return (
    <MemoryRouter>
      <EvidenceIds.Provider value={new Set(ids)}>{node}</EvidenceIds.Provider>
    </MemoryRouter>
  );
}

describe("Citation", () => {
  it("links a known evidence ID to its anchor", () => {
    render(withEvidence(["ev_0000000002"], <Citation id="ev_0000000002" />));
    expect(screen.getByRole("link", { name: "ev_0000000002" })).toHaveAttribute("href", "/#ev_0000000002");
  });

  it("shows an unknown ID without a link", () => {
    render(withEvidence([], <Citation id="ev_ffffffffff" />));
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("ev_ffffffffff")).toHaveAttribute("title", "本调查没有这条证据");
  });
});

describe("CallTable", () => {
  it("shows every call and the totals", () => {
    render(withEvidence(["ev_0000000002"], <CallTable data={callList} startedAt="2026-10-19T22:34:00Z" />));
    expect(screen.getAllByTestId("call-row")).toHaveLength(4);
    expect(screen.getByTestId("call-totals")).toHaveTextContent(
      "合计 4 次调用（模型 2 次、工具 1 次、知识检索 1 次）· token 38.2k · 总耗时 1 分 24 秒",
    );
    expect(screen.getByText("知识检索", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByText("拒绝：citation_integrity")).toBeInTheDocument();
  });
});

describe("CallTimeline", () => {
  it("opens a tool call first and switches to a clicked call", async () => {
    render(
      withEvidence(
        ["ev_0000000002"],
        <CallTimeline calls={callList.calls} startedAt="2026-10-19T22:34:00Z" elapsedMs={84000} />,
      ),
    );
    const detail = () => screen.getByTestId("call-detail");
    expect(detail()).toHaveTextContent("k8s_get_pod_status");
    expect(detail()).toHaveTextContent('{"pod": "x"}');
    await userEvent.click(screen.getByRole("button", { name: /^report，/ }));
    expect(detail()).toHaveTextContent("证据门拒绝：citation_integrity");
    expect(detail()).toHaveTextContent("模型调用只显示耗时、token 与证据门结果");
    await userEvent.click(screen.getByRole("button", { name: /^retrieve_knowledge，/ }));
    expect(detail()).toHaveTextContent("检索查询Bianque Pod Restarting");
    expect(detail()).toHaveTextContent("知识不是证据");
    expect(detail()).toHaveTextContent("kn_c73d8439117f");
  });
});
