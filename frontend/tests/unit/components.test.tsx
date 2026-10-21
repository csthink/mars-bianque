import { render, screen } from "@testing-library/react";
import { ReactFlowProvider, type NodeProps } from "@xyflow/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { AlertCard } from "../../src/components/AlertCard";
import { CallTable } from "../../src/components/CallTable";
import { CallTimeline } from "../../src/components/CallTimeline";
import { Citation } from "../../src/components/Citation";
import { CitableIds } from "../../src/components/citableContext";
import { StepCard, type StepNode } from "../../src/components/FlowGraph";
import { PlanPanel } from "../../src/components/PlanPanel";
import { callList, detail, plan, step } from "./fixtures";

function withEvidence(ids: string[], node: ReactNode) {
  return (
    <MemoryRouter>
      <CitableIds.Provider value={new Set(ids)}>{node}</CitableIds.Provider>
    </MemoryRouter>
  );
}

describe("Citation", () => {
  it("links a known evidence ID to its anchor", () => {
    render(withEvidence(["ev_0000000002"], <Citation id="ev_0000000002" />));
    expect(screen.getByRole("link", { name: "ev_0000000002" })).toHaveAttribute("href", "/#ev_0000000002");
  });

  it("links a suggestion a follow-up answer cites to the suggestion", () => {
    render(withEvidence(["sg_a710355d0c"], <Citation id="sg_a710355d0c" />));
    const link = screen.getByRole("link", { name: "sg_a710355d0c" });
    expect(link).toHaveAttribute("href", "/#sg_a710355d0c");
    expect(link).toHaveAttribute("title", "修复建议");
  });

  it("shows an unknown ID without a link", () => {
    render(withEvidence([], <Citation id="ev_ffffffffff" />));
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("ev_ffffffffff")).toHaveAttribute("title", "本调查没有这条证据");
  });
});

describe("AlertCard", () => {
  it("names the A2A client of an investigation an A2A task started", () => {
    render(withEvidence([], <AlertCard detail={detail({ trigger: "a2a", caller: "demo-agent" })} />));
    expect(screen.getByTestId("alert-card")).toHaveTextContent("触发：A2A");
    expect(screen.getByTestId("alert-card")).toHaveTextContent("调用方 demo-agent");
  });

  it("names no client otherwise", () => {
    render(withEvidence([], <AlertCard detail={detail({})} />));
    expect(screen.getByTestId("alert-card")).toHaveTextContent("触发：告警");
    expect(screen.getByTestId("alert-card")).not.toHaveTextContent("调用方");
  });
});

describe("CallTable", () => {
  it("shows every call and the totals", () => {
    render(withEvidence(["ev_0000000002"], <CallTable data={callList} startedAt="2026-10-19T22:34:00Z" />));
    expect(screen.getAllByTestId("call-row")).toHaveLength(5);
    expect(screen.getByTestId("call-totals")).toHaveTextContent(
      "合计 5 次调用（模型 2 次、工具 1 次、知识检索 1 次、skill 1 次）· token 38.2k · 总耗时 1 分 24 秒",
    );
    expect(screen.getByText("知识检索", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByText("skill", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByText("investigate-pod-restarts", { selector: "td" })).toBeInTheDocument();
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
    await userEvent.click(screen.getByRole("button", { name: /^investigate-pod-restarts，/ }));
    expect(detail()).toHaveTextContent("skill 名investigate-pod-restarts");
    expect(detail()).toHaveTextContent("首次加载");
    expect(detail()).toHaveTextContent("正文 SHA-2565d41402abc4b（前 12 位）");
    expect(detail()).toHaveTextContent("skill 只说明怎么查，不是证据");
  });

  it("names the loading of a skill again", async () => {
    const again = callList.calls.map((item) =>
      item.kind === "skill" ? { ...item, output_summary: "sha256 5d41402abc4b; already loaded" } : item,
    );
    render(withEvidence([], <CallTimeline calls={again} startedAt="2026-10-19T22:34:00Z" elapsedMs={84000} />));
    await userEvent.click(screen.getByRole("button", { name: /^investigate-pod-restarts，/ }));
    expect(screen.getByTestId("call-detail")).toHaveTextContent("已加载（本次调查之前加载过）");
  });
});

describe("PlanPanel", () => {
  it("lists the skills of the shown plan version", async () => {
    const plans = [
      plan(1, [step({ id: 1 })], "model", ["investigate-pod-restarts"]),
      plan(2, [step({ id: 2, domain: "change" })], "model", ["diagnose-config-error", "investigate-pod-restarts"]),
    ];
    render(withEvidence([], <PlanPanel plans={plans} assessments={[]} />));
    expect(screen.getByTestId("plan-skills")).toHaveTextContent("参考的 skillinvestigate-pod-restarts");
    await userEvent.click(screen.getByRole("tab", { name: "v2" }));
    expect(screen.getByTestId("plan-skills")).toHaveTextContent(
      "参考的 skilldiagnose-config-errorinvestigate-pod-restarts",
    );
  });

  it("shows no skill line for a plan made without skills", () => {
    render(withEvidence([], <PlanPanel plans={[plan(1, [step({ id: 1 })])]} assessments={[]} />));
    expect(screen.queryByTestId("plan-skills")).toBeNull();
  });
});

describe("StepCard", () => {
  const card = (version: number, overrides: Parameters<typeof step>[0]) =>
    render(
      <ReactFlowProvider>
        <StepCard {...({ data: { step: step(overrides), version, onSelect: () => {} } } as unknown as NodeProps<StepNode>)} />
      </ReactFlowProvider>,
    );

  it("keeps the number and the status of a supplementary step on one header line", () => {
    card(2, { seq: 2, status: "running" });
    const number = screen.getByText("v2 · 2");
    expect(number).toHaveClass("whitespace-nowrap", "text-violet-700");
    expect(number).toHaveAttribute("title", "第 2 轮（补充计划）");
    expect(screen.getByText("进行中")).toHaveClass("whitespace-nowrap");
  });

  it("shows a step of the first plan in the plain colour", () => {
    card(1, { seq: 1 });
    const number = screen.getByText("v1 · 1");
    expect(number).not.toHaveClass("text-violet-700");
    expect(number).not.toHaveAttribute("title");
  });
});
