import { expect, test, type APIRequestContext } from "@playwright/test";
import { detail, freshInvestigation, login, seededInvestigation } from "./support";

// Full-page screenshots for the layout review: wide and narrow, light and dark. They
// land in e2e/screenshots/ (ignored by Git) and are uploaded by CI.
const widths = [1440, 390];
const schemes = ["light", "dark"] as const;

/** An investigation with a decided suggestion and an answered question. */
async function worked(request: APIRequestContext): Promise<string> {
  const id = await freshInvestigation(request);
  const suggestion = (await detail(request, id)).suggestions[0];
  await request.post(`/api/investigations/${id}/suggestions/${suggestion?.id}/decision`, {
    data: { decision: "confirmed", comment: "演示确认" },
  });
  const session = (await (await request.post(`/api/investigations/${id}/sessions`)).json()) as { id: number };
  const answer = await request.post(`/api/sessions/${session.id}/messages`, {
    data: { question: "Why this commit?" },
  });
  expect(await answer.text()).toContain("event: done");
  return id;
}

const pages: { name: string; path: (request: APIRequestContext) => Promise<string>; loggedIn: boolean }[] = [
  { name: "login", path: async () => "/login?next=%2F", loggedIn: false },
  { name: "list", path: async () => "/", loggedIn: true },
  { name: "detail", path: async (request) => `/investigations/${await seededInvestigation(request)}`, loggedIn: false },
  { name: "detail-worked", path: async (request) => `/investigations/${await worked(request)}`, loggedIn: true },
];

for (const { name, path, loggedIn } of pages) {
  for (const width of widths) {
    for (const scheme of schemes) {
      test(`screenshot ${name} ${width} ${scheme}`, async ({ page }) => {
        if (loggedIn) await login(page.request);
        const target = await path(page.request);
        await page.setViewportSize({ width, height: 900 });
        await page.emulateMedia({ colorScheme: scheme });
        await page.goto(target);
        await page.getByRole("heading").first().waitFor();
        await page.waitForLoadState("networkidle");
        await page.screenshot({ path: `e2e/screenshots/${name}-${width}-${scheme}.png`, fullPage: true });
      });
    }
  }
}
