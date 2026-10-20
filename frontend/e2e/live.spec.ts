import { expect, test } from "@playwright/test";
import { login, trigger } from "./support";

// S1-d replays a log tool that times out and is retried: in scripted mode the
// investigation takes about six seconds, while the kubernetes steps end within the
// first. The page must show step events while the investigation is still running.

test("progress events reach the page before the investigation ends", async ({ page }) => {
  await login(page.request);
  const id = await trigger(page.request, "s1-d");
  await page.goto(`/investigations/${id}`);

  const seen = await page.waitForFunction(
    () => {
      const steps = document.querySelectorAll("[data-testid=progress-event][data-type^=step_]");
      const moved = document.querySelectorAll("[data-testid=flow-step]:is([data-status=running], [data-status=done])");
      const status = document.querySelector("[data-testid=investigation-status]")?.getAttribute("data-status");
      return steps.length > 0 && moved.length > 0 ? status : false;
    },
    undefined,
    { timeout: 15_000 },
  );
  expect(await seen.jsonValue()).toBe("running");
  await expect(page.getByTestId("investigation-status")).toContainText("进行中 · 第");
  await page.screenshot({ path: "e2e/screenshots/detail-running-1440-light.png" });

  await expect(page.getByTestId("investigation-status")).toHaveAttribute("data-status", /^(reported|degraded)$/, {
    timeout: 30_000,
  });
  await expect(page.getByTestId("stream-state")).toHaveText("已结束");
  await expect(page.getByTestId("progress-event").last()).toContainText("调查结束");
});
