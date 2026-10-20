import { test } from "@playwright/test";
import { detailPath } from "./fixture";

// Full-page screenshots for the layout review: wide and narrow, light and dark. They
// land in e2e/screenshots/ (ignored by Git) and are uploaded by CI.
const pages = [
  { name: "list", path: "/" },
  { name: "detail", path: detailPath },
];
const widths = [1440, 390];
const schemes = ["light", "dark"] as const;

for (const { name, path } of pages) {
  for (const width of widths) {
    for (const scheme of schemes) {
      test(`screenshot ${name} ${width} ${scheme}`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await page.emulateMedia({ colorScheme: scheme });
        await page.goto(path);
        await page.getByRole("heading").first().waitFor();
        await page.waitForLoadState("networkidle");
        await page.screenshot({ path: `e2e/screenshots/${name}-${width}-${scheme}.png`, fullPage: true });
      });
    }
  }
}
