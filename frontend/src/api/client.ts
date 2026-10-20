import { useQuery } from "@tanstack/react-query";
import type { CallList, InvestigationDetail, InvestigationSummary } from "./types";

export class NotFoundError extends Error {}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (response.status === 404) throw new NotFoundError(path);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return (await response.json()) as T;
}

export function useInvestigations() {
  return useQuery({
    queryKey: ["investigations"],
    queryFn: () =>
      getJson<{ investigations: InvestigationSummary[] }>("/api/investigations").then(
        (body) => body.investigations,
      ),
  });
}

export function useInvestigation(id: string) {
  return useQuery({
    queryKey: ["investigation", id],
    queryFn: () => getJson<InvestigationDetail>(`/api/investigations/${encodeURIComponent(id)}`),
  });
}

export function useCalls(id: string) {
  return useQuery({
    queryKey: ["calls", id],
    queryFn: () => getJson<CallList>(`/api/investigations/${encodeURIComponent(id)}/calls`),
  });
}
