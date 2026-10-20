import { expect, test } from "@playwright/test";
import { detail, freshInvestigation } from "./support";

test("a suggestion is confirmed or rejected once, and only recorded", async ({ page }) => {
  const id = await freshInvestigation(page.request);
  await page.goto(`/investigations/${id}`);
  const [change, readOnly] = [page.getByTestId("suggestion").nth(0), page.getByTestId("suggestion").nth(1)];
  await expect(change).toContainText("变更");
  await expect(change).toContainText("待确认");
  await expect(change).toContainText("只记录决定，不执行命令");

  await change.getByRole("button", { name: "确认" }).click();
  await change.getByLabel("确认说明（可留空）").fill("演示确认");
  await change.getByRole("button", { name: "提交确认" }).click();
  await expect(change.getByTestId("decision")).toContainText("已确认 · 操作人 demo");
  await expect(change.getByTestId("decision")).toContainText("说明：演示确认");
  await expect(change.getByRole("button")).toHaveCount(0);

  await readOnly.getByRole("button", { name: "驳回" }).click();
  await readOnly.getByRole("button", { name: "提交驳回" }).click();
  await expect(readOnly.getByTestId("decision")).toContainText("已驳回 · 操作人 demo");

  // The decisions are what the backend stored; a reload shows the same.
  await page.reload();
  await expect(page.locator("[data-testid=suggestion][data-state=confirmed]")).toHaveCount(1);
  await expect(page.locator("[data-testid=suggestion][data-state=rejected]")).toHaveCount(1);
  const stored = (await detail(page.request, id)).suggestions.map((item) => [item.state, item.decision_comment]);
  expect(stored).toEqual([
    ["confirmed", "演示确认"],
    ["rejected", null],
  ]);
});

test("a decision someone else made first is shown and not overwritten", async ({ page }) => {
  const id = await freshInvestigation(page.request);
  await page.goto(`/investigations/${id}`);
  const first = page.getByTestId("suggestion").first();
  await first.getByRole("button", { name: "确认" }).click();
  const suggestionId = (await detail(page.request, id)).suggestions[0]?.id;
  const elsewhere = await page.request.post(`/api/investigations/${id}/suggestions/${suggestionId}/decision`, {
    data: { decision: "rejected", comment: "在别处驳回" },
  });
  expect(elsewhere.status()).toBe(201);

  await first.getByRole("button", { name: "提交确认" }).click();
  await expect(first.getByRole("status")).toHaveText("已由他人决定，未改动。");
  await expect(first.getByTestId("decision")).toContainText("已驳回 · 操作人 demo");
  await expect(first.getByTestId("decision")).toContainText("说明：在别处驳回");
});
