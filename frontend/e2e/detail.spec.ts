import { expect, test } from "@playwright/test";
import type { CallList } from "../src/api/types";
import { detail, seededInvestigation } from "./support";

// Reads only: the S1-a investigation that the backend ran in scripted mode at startup.

test("the list links to the investigation", async ({ page }) => {
  const id = await seededInvestigation(page.request);
  await page.goto("/");
  const row = page.getByTestId("investigation-row").filter({ hasText: id });
  await expect(row).toContainText("BianquePodRestarting");
  await expect(row).toContainText("正式报告");
  await row.getByRole("link", { name: "BianquePodRestarting" }).click();
  await expect(page).toHaveURL(new RegExp(`/investigations/${id}$`));
});

test("the detail page shows every section", async ({ page }) => {
  const id = await seededInvestigation(page.request);
  const data = await detail(page.request, id);
  await page.goto(`/investigations/${id}`);
  for (const section of [
    "alert-card",
    "flow",
    "plan-panel",
    "call-timeline",
    "evidence-list",
    "report",
    "suggestions",
    "followup",
    "call-table",
  ]) {
    await expect(page.getByTestId(section)).toBeVisible();
  }
  await expect(page.getByTestId("alert-card")).toContainText("BianquePodRestarting");
  await expect(page.getByTestId("investigation-status")).toHaveText("已报告");
  await expect(page.getByTestId("assessment")).toHaveCount(data.assessments.length);
  await expect(page.getByTestId("evidence-item")).toHaveCount(data.evidence.length);
  await expect(page.getByTestId("suggestion")).toHaveCount(data.suggestions.length);

  const [first, second] = data.plans;
  await expect(page.getByTestId("plan-step")).toHaveCount(first?.steps.length ?? 0);
  await page.getByRole("tab", { name: "v2" }).click();
  await expect(page.getByTestId("plan-step")).toHaveCount(second?.steps.length ?? 0);
  await expect(page.getByTestId("plan-step").first()).toContainText("change");
});

test("the flow graph has one node per plan step, in the lanes of the sub-agents", async ({ page }) => {
  const id = await seededInvestigation(page.request);
  const data = await detail(page.request, id);
  await page.goto(`/investigations/${id}`);
  const steps = data.plans.flatMap((plan) => plan.steps);
  await expect(page.getByTestId("flow-step")).toHaveCount(steps.length);
  await expect(page.locator("[data-testid=flow-step][data-status=done]")).toHaveCount(steps.length);
  const graph = page.getByTestId("flow-graph");
  for (const lane of ["kubernetes", "logs", "change"]) await expect(graph).toContainText(lane);
  await expect(graph).toContainText("第 2 轮 · 计划 v2（补充）");

  // A step of the second round opens its version in the plan panel.
  await page.getByTestId("flow-step").filter({ hasText: "v2 · 1" }).getByRole("button").click();
  await expect(page.getByRole("tab", { name: "v2" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("[data-testid=plan-step][data-focused=true]")).toBeInViewport();
});

test("the progress events of a finished investigation are listed and the stream is closed", async ({ page }) => {
  const id = await seededInvestigation(page.request);
  await page.goto(`/investigations/${id}`);
  await expect(page.getByTestId("stream-state")).toHaveText("已结束");
  await expect(page.getByTestId("progress-event").last()).toHaveText(/调查结束（已报告）$/);
  await page.getByRole("button", { name: /^展开全部 \d+ 条$/ }).click();
  await expect(page.getByTestId("progress-event").first()).toHaveText(/调查开始（回放）$/);
});

test("tool calls are grouped by agent and open their details", async ({ page }) => {
  const id = await seededInvestigation(page.request);
  await page.goto(`/investigations/${id}`);
  const rows = page.getByTestId("timeline-row");
  await expect(rows).toHaveCount(4);
  expect(await rows.evaluateAll((items) => items.map((item) => item.getAttribute("data-agent")))).toEqual([
    "investigation",
    "kubernetes",
    "logs",
    "change",
  ]);
  await page.getByRole("button", { name: /^change_show_commit，/ }).click();
  const callDetail = page.getByTestId("call-detail");
  await expect(callDetail).toContainText("change_show_commit");
  await expect(callDetail).toContainText("重试 0 次");
});

test("a citation in the report jumps to its evidence", async ({ page }) => {
  const id = await seededInvestigation(page.request);
  await page.goto(`/investigations/${id}`);
  const citation = page.getByTestId("root-cause").locator("[data-citation]").first();
  const evidenceId = await citation.getAttribute("data-citation");
  expect(evidenceId).toMatch(/^ev_[0-9a-f]{10}$/);
  await citation.click();
  await expect(page).toHaveURL(new RegExp(`#${evidenceId}$`));
  const target = page.locator(`[data-testid="evidence-item"][id="${evidenceId}"]`);
  await expect(target).toHaveAttribute("data-highlighted", "true");
  await expect(target).toBeInViewport();
});

test("the call table lists every call with token and time totals", async ({ page }) => {
  const id = await seededInvestigation(page.request);
  const calls = (await (await page.request.get(`/api/investigations/${id}/calls`)).json()) as CallList;
  const { totals } = calls;
  await page.goto(`/investigations/${id}`);
  await expect(page.getByTestId("call-row")).toHaveCount(calls.calls.length);
  const tokens = ((totals.tokens_in + totals.tokens_out) / 1000).toFixed(1);
  await expect(page.getByTestId("call-totals")).toContainText(
    `合计 ${totals.calls} 次调用（模型 ${totals.model_calls} 次、工具 ${totals.tool_calls} 次、知识检索 ${totals.knowledge_calls} 次）· token ${tokens}k · 总耗时`,
  );
  await expect(page.getByTestId("investigation-stats")).toContainText(`${totals.calls} 次调用`);
});

test("an unknown investigation shows a not-found message", async ({ page }) => {
  await page.goto("/investigations/inv_missing");
  await expect(page.getByTestId("load-error")).toContainText("找不到调查 inv_missing");
});
