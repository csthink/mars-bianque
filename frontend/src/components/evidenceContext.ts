import { createContext, useContext } from "react";

/** The evidence IDs of the investigation on the page; citations check against it. */
export const EvidenceIds = createContext<ReadonlySet<string>>(new Set());

export const useEvidenceIds = () => useContext(EvidenceIds);
