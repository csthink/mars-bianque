import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The investigation that `make e2e` loads into the database before the tests run.
const path = fileURLToPath(new URL("../../backend/tests/fixtures/investigation/s1a.json", import.meta.url));

interface FixtureCall {
  kind: "model" | "tool";
  tokens_in: number | null;
  tokens_out: number | null;
}

export const fixture = JSON.parse(readFileSync(path, "utf8")) as {
  investigation: { id: string };
  evidence: { id: string }[];
  calls: FixtureCall[];
  plans: { version: number; steps: { domain: string }[] }[];
};

export const detailPath = `/investigations/${fixture.investigation.id}`;
