import { createContext, useContext } from "react";

/**
 * The IDs a citation on the page can point at: the investigation's evidence, and its
 * suggestions, which follow-up answers cite when they speak of remediation.
 */
export const CitableIds = createContext<ReadonlySet<string>>(new Set());

export const useCitableIds = () => useContext(CitableIds);
