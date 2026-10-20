import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import type {
  CallList,
  FollowupSessionDetail,
  FollowupSessionSummary,
  FollowupSessionView,
  InvestigationDetail,
  InvestigationSummary,
  QuestionList,
  SessionUser,
  SuggestionView,
} from "./types";

/** A response that is not a success, with its parsed JSON body when it has one. */
export class RequestError extends Error {
  constructor(
    readonly path: string,
    readonly status: number,
    readonly body: unknown,
  ) {
    super(`${path}: HTTP ${status}`);
  }
}

export class NotFoundError extends RequestError {}

/** 401: no valid session. */
export class UnauthorizedError extends RequestError {}

/** 409: the request conflicts with the current state, for example a decided suggestion. */
export class ConflictError extends RequestError {}

/** 422: the request was understood and refused, for example an unrecorded question. */
export class UnprocessableError extends RequestError {}

const ERRORS: Record<number, typeof RequestError> = {
  401: UnauthorizedError,
  404: NotFoundError,
  409: ConflictError,
  422: UnprocessableError,
};

/** The error a non-success response stands for. */
export async function errorOf(path: string, response: Response): Promise<RequestError> {
  const body: unknown = await response.json().catch(() => null);
  return new (ERRORS[response.status] ?? RequestError)(path, response.status, body);
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw await errorOf(path, response);
  return (await response.json()) as T;
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw await errorOf(path, response);
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

const investigationPath = (id: string) => `/api/investigations/${encodeURIComponent(id)}`;

/** How often the list is read again: it has no event stream. */
export const LIST_REFRESH_MS = 5000;

export function useInvestigations() {
  return useQuery({
    queryKey: ["investigations"],
    queryFn: () =>
      getJson<{ investigations: InvestigationSummary[] }>("/api/investigations").then(
        (body) => body.investigations,
      ),
    refetchInterval: LIST_REFRESH_MS,
  });
}

export function useInvestigation(id: string) {
  return useQuery({
    queryKey: ["investigation", id],
    queryFn: () => getJson<InvestigationDetail>(investigationPath(id)),
  });
}

export function useCalls(id: string) {
  return useQuery({
    queryKey: ["calls", id],
    queryFn: () => getJson<CallList>(`${investigationPath(id)}/calls`),
  });
}

// Login. The session cookie is HttpOnly, so the page asks who is logged in.

const SESSION_KEY = ["auth-session"];

/** The logged-in user, or ``null`` when nobody is. */
export function useSession() {
  return useQuery({
    queryKey: SESSION_KEY,
    queryFn: async () => {
      try {
        return await getJson<SessionUser>("/api/auth/session");
      } catch (error) {
        if (error instanceof UnauthorizedError) return null;
        throw error;
      }
    },
    retry: false,
    staleTime: Infinity,
  });
}

/** Set the login state the pages show; ``null`` after a logout or an expired session. */
export function useSetSession() {
  const queryClient = useQueryClient();
  return useCallback(
    (user: SessionUser | null) => queryClient.setQueryData(SESSION_KEY, user),
    [queryClient],
  );
}

/**
 * Handle an error of a write: a 401 means the session expired, so the page shows the
 * login prompts again. Returns whether it was that.
 */
export function useUnauthorizedHandler() {
  const setSession = useSetSession();
  return useCallback(
    (error: unknown) => {
      if (!(error instanceof UnauthorizedError)) return false;
      setSession(null);
      return true;
    },
    [setSession],
  );
}

export const login = (username: string, password: string) =>
  postJson<SessionUser>("/api/auth/login", { username, password });

export const logout = () => postJson<undefined>("/api/auth/logout");

// Suggestion decisions.

export const decide = (
  investigationId: string,
  suggestionId: string,
  decision: "confirmed" | "rejected",
  comment: string,
) =>
  postJson<SuggestionView>(
    `${investigationPath(investigationId)}/suggestions/${encodeURIComponent(suggestionId)}/decision`,
    { decision, comment: comment.trim() === "" ? null : comment },
  );

// Follow-up sessions.

export function useSessions(investigationId: string) {
  return useQuery({
    queryKey: ["followup-sessions", investigationId],
    queryFn: () =>
      getJson<{ sessions: FollowupSessionSummary[] }>(`${investigationPath(investigationId)}/sessions`).then(
        (body) => body.sessions,
      ),
  });
}

export function useFollowupSession(sessionId: number | null) {
  return useQuery({
    queryKey: ["followup-session", sessionId],
    queryFn: () => getJson<FollowupSessionDetail>(`/api/sessions/${sessionId}`),
    enabled: sessionId !== null,
  });
}

export function useQuestions(investigationId: string) {
  return useQuery({
    queryKey: ["questions", investigationId],
    queryFn: () => getJson<QuestionList>(`${investigationPath(investigationId)}/questions`),
  });
}

export const createSession = (investigationId: string) =>
  postJson<FollowupSessionView>(`${investigationPath(investigationId)}/sessions`);
