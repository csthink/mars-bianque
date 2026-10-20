import { expect, test } from "@playwright/test";
import { detailPath, fixture } from "./fixture";

test("the list links to the investigation", async ({ page }) => {
  await page.goto("/");
  const row = page.getByTestId("investigation-row").filter({ hasText: fixture.investigation.id });
  await expect(row).toContainText("BianquePodRestarting");
  await expect(row).toContainText("正式报告");
  await row.getByRole("link", { name: "BianquePodRestarting" }).click();
  await expect(page).toHaveURL(new RegExp(`${detailPath}$`));
});

test("the detail page shows every read-only section", async ({ page }) => {
  await page.goto(detailPath);
  for (const id of ["alert-card", "plan-panel", "call-timeline", "evidence-list", "report", "suggestions", "call-table"]) {
    await expect(page.getByTestId(id)).toBeVisible();
  }
  await expect(page.getByTestId("alert-card")).toContainText("BianquePodRestarting");
  await expect(page.getByTestId("assessment")).toHaveCount(2);
  await expect(page.getByTestId("evidence-item")).toHaveCount(fixture.evidence.length);
  await expect(page.getByTestId("rejected-attempts")).toHaveText("第 1 次生成被拒：citation_integrity（只显示原因）");

  const firstPlan = fixture.plans[0]?.steps.length ?? 0;
  await expect(page.getByTestId("plan-step")).toHaveCount(firstPlan);
  await page.getByRole("tab", { name: "v2" }).click();
  const secondPlan = fixture.plans[1]?.steps ?? [];
  await expect(page.getByTestId("plan-step")).toHaveCount(secondPlan.length);
  await expect(page.getByTestId("plan-step").first()).toContainText("change");
});

test("tool calls are grouped by agent and open their details", async ({ page }) => {
  await page.goto(detailPath);
  const rows = page.getByTestId("timeline-row");
  await expect(rows).toHaveCount(4);
  expect(await rows.evaluateAll((items) => items.map((item) => item.getAttribute("data-agent")))).toEqual([
    "investigation",
    "kubernetes",
    "logs",
    "change",
  ]);
  await page.getByRole("button", { name: /^change_show_commit，/ }).click();
  const detail = page.getByTestId("call-detail");
  await expect(detail).toContainText("change_show_commit");
  await expect(detail).toContainText("重试 0 次");
  await expect(detail).toContainText('"short": "9b9a6ea8a72b"');
  await page.getByRole("button", { name: /^retrieve_knowledge，/ }).click();
  await expect(detail).toContainText("检索到的运维手册");
  await expect(detail).toContainText("kn_c73d8439117f CrashLoopBackOff > Reading the previous container log");
});

test("a citation in the report jumps to its evidence", async ({ page }) => {
  await page.goto(detailPath);
  const citation = page.getByTestId("root-cause").locator("[data-citation]").first();
  const id = await citation.getAttribute("data-citation");
  expect(id).toMatch(/^ev_[0-9a-f]{10}$/);
  await citation.click();
  await expect(page).toHaveURL(new RegExp(`#${id}$`));
  const target = page.locator(`[data-testid="evidence-item"][id="${id}"]`);
  await expect(target).toHaveAttribute("data-highlighted", "true");
  await expect(target).toBeInViewport();
});

test("the call table lists every call with token and time totals", async ({ page }) => {
  await page.goto(detailPath);
  await expect(page.getByTestId("call-row")).toHaveCount(fixture.calls.length);
  const model = fixture.calls.filter((call) => call.kind === "model");
  const knowledge = fixture.calls.filter((call) => call.kind === "knowledge");
  const tools = fixture.calls.length - model.length - knowledge.length;
  const tokens = model.reduce((sum, call) => sum + (call.tokens_in ?? 0) + (call.tokens_out ?? 0), 0);
  await expect(page.getByTestId("call-totals")).toHaveText(
    `合计 ${fixture.calls.length} 次调用（模型 ${model.length} 次、工具 ${tools} 次、知识检索 ${knowledge.length} 次）· token ${(tokens / 1000).toFixed(1)}k · 总耗时 1 分 24 秒`,
  );
  await expect(page.getByTestId("investigation-stats")).toHaveText(`2 轮 · ${fixture.calls.length} 次调用 · ${(tokens / 1000).toFixed(1)}k token · 1 分 24 秒`);
});

test("an unknown investigation shows a not-found message", async ({ page }) => {
  await page.goto("/investigations/inv_missing");
  await expect(page.getByTestId("load-error")).toContainText("找不到调查 inv_missing");
});
