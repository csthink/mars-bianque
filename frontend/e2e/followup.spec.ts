import { expect, test } from "@playwright/test";
import { freshInvestigation } from "./support";

interface AnswerState {
  stage: string;
  segments: number;
  text: string;
}

test("an answer shows progress only, then its paragraphs with citations, and is kept", async ({ page }) => {
  const id = await freshInvestigation(page.request);
  await page.goto(`/investigations/${id}`);
  const panel = page.getByTestId("followup");
  await expect(panel).toContainText("围绕报告提问");

  // Record every state the pending answer goes through.
  await page.evaluate(() => {
    const log: AnswerState[] = [];
    (window as unknown as { answerLog: AnswerState[] }).answerLog = log;
    const record = () => {
      const answer = document.querySelector<HTMLElement>("[data-testid=pending-answer]");
      if (!answer) return;
      const text = [...answer.querySelectorAll("[data-testid=answer-segment]")].map((p) => p.textContent).join("");
      log.push({ stage: answer.dataset.stage ?? "", segments: Number(answer.dataset.segments), text });
    };
    new MutationObserver(record).observe(document.body, { subtree: true, childList: true, attributes: true });
  });

  await panel.getByLabel("问题").fill("Why this commit?");
  await panel.getByRole("button", { name: "提问" }).click();
  const answer = panel.getByTestId("followup-answer");
  await expect(answer).toBeVisible();
  await expect(panel.getByTestId("pending-answer")).toHaveCount(0);

  const log = await page.evaluate(() => (window as unknown as { answerLog: AnswerState[] }).answerLog);
  const firstText = log.findIndex((state) => state.segments > 0);
  expect(firstText).toBeGreaterThan(0);
  const before = log.slice(0, firstText);
  expect(before.every((state) => state.text === "")).toBe(true);
  expect(before.map((state) => state.stage)).toContain("validating");

  const segments = answer.getByTestId("answer-segment");
  expect(await segments.count()).toBeGreaterThan(0);
  const citation = answer.locator("[data-citation]").first();
  const evidenceId = await citation.getAttribute("data-citation");
  await citation.click();
  await expect(page.locator(`[data-testid="evidence-item"][id="${evidenceId}"]`)).toHaveAttribute(
    "data-highlighted",
    "true",
  );

  // An answer about remediation cites the suggestion; the citation jumps to it.
  const suggestionId = await answer.locator("[data-citation^=sg_]").first().getAttribute("data-citation");
  await answer.locator(`[data-citation="${suggestionId}"]`).click();
  await expect(page.locator(`[data-testid="suggestion"][id="${suggestionId}"]`)).toHaveAttribute(
    "data-highlighted",
    "true",
  );

  // The conversation is stored: after a reload the session opens with its turn.
  await page.reload();
  await expect(panel.getByTestId("followup-question")).toHaveText("Why this commit?");
  await expect(panel.getByTestId("followup-answer")).toBeVisible();
  await expect(panel.getByRole("combobox", { name: "追问会话" })).toContainText("1 轮");
});
