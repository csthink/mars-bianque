import { expect, type APIRequestContext } from "@playwright/test";
import type { InvestigationDetail, InvestigationSummary } from "../src/api/types";

// The replay stack of `make e2e` runs in scripted model mode: once started it has
// investigated every case, and tests trigger more investigations of their own.

/** The demo password `make e2e` generated for this run. */
export function password(): string {
  const value = process.env.BIANQUE_E2E_PASSWORD;
  if (!value) throw new Error("BIANQUE_E2E_PASSWORD is not set; run the tests with make e2e");
  return value;
}

/** Log the context in; a page's ``request`` shares its cookies with the page. */
export async function login(request: APIRequestContext) {
  const response = await request.post("/api/auth/login", { data: { username: "demo", password: password() } });
  expect(response.status()).toBe(200);
}

export async function detail(request: APIRequestContext, id: string): Promise<InvestigationDetail> {
  const response = await request.get(`/api/investigations/${id}`);
  expect(response.ok()).toBe(true);
  return (await response.json()) as InvestigationDetail;
}

export async function investigations(request: APIRequestContext): Promise<InvestigationSummary[]> {
  const response = await request.get("/api/investigations?limit=200");
  expect(response.ok()).toBe(true);
  return ((await response.json()) as { investigations: InvestigationSummary[] }).investigations;
}

export const ENDED = ["reported", "degraded", "failed", "interrupted"];

/** Start an investigation of a case; the context must be logged in. */
export async function trigger(request: APIRequestContext, caseId: string): Promise<string> {
  const response = await request.post("/api/investigations", { data: { case_id: caseId } });
  expect(response.status()).toBe(202);
  return ((await response.json()) as { investigation_id: string }).investigation_id;
}

export async function waitEnded(request: APIRequestContext, id: string): Promise<InvestigationDetail> {
  await expect
    .poll(async () => (await detail(request, id)).investigation.status, { timeout: 30_000 })
    .toMatch(new RegExp(`^(${ENDED.join("|")})$`));
  return detail(request, id);
}

/** A new, finished investigation of a case that no other test touches. */
export async function freshInvestigation(request: APIRequestContext, caseId = "s1-a"): Promise<string> {
  await login(request);
  const id = await trigger(request, caseId);
  expect((await waitEnded(request, id)).investigation.status).toBe("reported");
  return id;
}

/** The S1-a investigation the backend ran at startup; tests only read it. */
export async function seededInvestigation(request: APIRequestContext, caseId = "s1-a"): Promise<string> {
  const items = (await investigations(request)).filter((item) => item.case_id === caseId);
  const first = items.sort((a, b) => a.started_at.localeCompare(b.started_at))[0];
  if (!first) throw new Error(`no investigation of ${caseId}`);
  return first.id;
}
