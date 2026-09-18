/**
 * API client.
 *
 * One place that knows about the base URL, the bearer token and the backend's
 * error envelope, so no component ever touches `fetch` directly.
 */

import type {
  AgentRun,
  AgentRunSummary,
  ApiError,
  CurrentUser,
  DocumentRecord,
  EvaluationCase,
  EvaluationResult,
  Page,
  SuiteSummary,
  TokenResponse,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const PREFIX = `${BASE}/api/v1`;
const TOKEN_KEY = "sra.access_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class RequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
    readonly requestId: string | null = null,
  ) {
    super(message);
    this.name = "RequestError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${PREFIX}${path}`, { ...init, headers });

  if (response.status === 401) {
    setToken(null);
    throw new RequestError(401, "authentication_failed", "Your session has expired.");
  }

  if (!response.ok) {
    let payload: ApiError | null = null;
    try {
      payload = (await response.json()) as ApiError;
    } catch {
      // Non-JSON error body (a proxy, a gateway timeout). Fall through.
    }
    throw new RequestError(
      response.status,
      payload?.error.code ?? "http_error",
      payload?.error.message ?? `Request failed with ${response.status}`,
      payload?.error.details ?? {},
      payload?.request_id ?? null,
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  // --- auth ---------------------------------------------------------------
  async login(email: string, password: string): Promise<TokenResponse> {
    const tokens = await request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    setToken(tokens.access_token);
    return tokens;
  },
  logout(): void {
    setToken(null);
  },
  me: () => request<CurrentUser>("/me"),

  // --- agent --------------------------------------------------------------
  chat: (question: string, options: Record<string, unknown> = {}) =>
    request<AgentRun>("/chat", {
      method: "POST",
      body: JSON.stringify({ question, ...options }),
    }),
  listRuns: (limit = 25) => request<Page<AgentRunSummary>>(`/agent/runs?limit=${limit}`),
  getRun: (runId: string) => request<AgentRun>(`/agent/runs/${runId}`),
  decideApproval: (runId: string, approved: boolean, note?: string) =>
    request<AgentRun>(`/agent/runs/${runId}/approve`, {
      method: "POST",
      body: JSON.stringify({ approved, note: note ?? null }),
    }),

  // --- documents ----------------------------------------------------------
  listDocuments: (limit = 50) => request<Page<DocumentRecord>>(`/documents?limit=${limit}`),
  getDocument: (id: string) => request<DocumentRecord>(`/documents/${id}`),
  uploadDocument: (file: File, fields: Record<string, string> = {}) => {
    const form = new FormData();
    form.append("file", file);
    for (const [key, value] of Object.entries(fields)) form.append(key, value);
    return request<{ document_id: string; status: string; message: string }>("/documents", {
      method: "POST",
      body: form,
    });
  },

  // --- evaluation ---------------------------------------------------------
  evaluationCases: () => request<EvaluationCase[]>("/evaluation/cases"),
  evaluationResults: (limit = 100) =>
    request<EvaluationResult[]>(`/evaluation/results?limit=${limit}`),
  evaluationSummary: () => request<SuiteSummary[]>("/evaluation/summary"),
  runEvaluation: (suite = "core", useJudge = true) =>
    request<{ run_label: string; task_id: string | null }>("/evaluation/run", {
      method: "POST",
      body: JSON.stringify({ suite, use_judge: useJudge, async_mode: true }),
    }),
};
