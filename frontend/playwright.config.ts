import { defineConfig, devices } from "@playwright/test";

// Runs against the replay stack started by `make e2e` (nginx in front of the backend).
export default defineConfig({
  testDir: "e2e",
  outputDir: "test-results",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.BIANQUE_E2E_URL ?? "http://127.0.0.1:8080",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
});
