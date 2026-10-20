import { expect, test } from "@playwright/test";
import { password, seededInvestigation } from "./support";

test("logging in returns to the page it started from, and logging out hides the write controls", async ({
  page,
}) => {
  const id = await seededInvestigation(page.request);
  await page.goto(`/investigations/${id}`);
  await expect(page.getByTestId("suggestion").first().getByTestId("login-prompt")).toHaveText("登录后可确认或驳回");
  await expect(page.getByTestId("followup").getByTestId("login-prompt")).toHaveText("登录后可追问");

  await page.getByTestId("suggestion").first().getByRole("link", { name: "登录" }).click();
  await expect(page).toHaveURL(new RegExp(`/login\\?next=${encodeURIComponent(`/investigations/${id}`)}$`));
  await page.getByLabel("用户名").fill("demo");
  await page.getByLabel("口令").fill("not-the-password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("alert")).toHaveText("用户名或口令不对。");

  await page.getByLabel("口令").fill(password());
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(new RegExp(`/investigations/${id}$`));
  await expect(page.getByTestId("session-user")).toHaveText("demo");
  await expect(page.getByTestId("suggestion").first().getByRole("button", { name: "确认" })).toBeVisible();
  await expect(page.getByLabel("问题")).toBeVisible();

  await page.getByRole("button", { name: "登出" }).click();
  await expect(page.getByRole("link", { name: "登录" }).first()).toBeVisible();
  await expect(page.getByTestId("followup").getByTestId("login-prompt")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("session-user")).toHaveCount(0);
});
