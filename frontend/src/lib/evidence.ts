import type { EvidenceView } from "../api/types";

/** Evidence in the order it was collected; the alert comes first. */
export function byCollection(evidence: EvidenceView[]): EvidenceView[] {
  return [...evidence].sort(
    (a, b) => a.collected_at.localeCompare(b.collected_at) || a.id.localeCompare(b.id),
  );
}
