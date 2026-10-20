import { request, type FullConfig } from "@playwright/test";
import { ENDED } from "./support";

/** The backend investigates the six cases once it has started: wait until all have ended. */
export default async function globalSetup(config: FullConfig) {
  const baseURL = config.projects[0]?.use.baseURL;
  const context = await request.newContext({ baseURL });
  const deadline = Date.now() + 120_000;
  try {
    for (;;) {
      const response = await context.get("/api/investigations");
      const items = response.ok()
        ? ((await response.json()) as { investigations: { status: string }[] }).investigations
        : [];
      if (items.length >= 6 && items.every((item) => ENDED.includes(item.status))) return;
      if (Date.now() > deadline) throw new Error(`startup investigations did not end: ${JSON.stringify(items)}`);
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  } finally {
    await context.dispose();
  }
}
