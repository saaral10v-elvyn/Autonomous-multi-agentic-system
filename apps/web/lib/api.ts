const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type User = { id: string; email: string; created_at: string };
export type Provider = "anthropic" | "openai" | "gemini";
export type ProviderKey = {
  id: string;
  provider: Provider;
  label: string;
  last4: string;
  created_at: string;
};
export type ProviderTest = {
  ok: boolean;
  provider: Provider;
  model: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: string;
};
export type AutonomyLevel = "step_by_step" | "milestones" | "autopilot";
export type ApprovalRisk = "read" | "sandbox_write" | "reversible_external" | "irreversible";
export type ApprovalStatus = "pending" | "approved" | "rejected" | "edited" | "expired";
export type Approval = {
  id: string;
  run_id: string;
  agent_id: string | null;
  action_type: string;
  risk: ApprovalRisk;
  payload: Record<string, unknown>;
  reason: string;
  estimated_cost_usd: string | null;
  rollback_info: Record<string, unknown> | null;
  status: ApprovalStatus;
  edited_payload: Record<string, unknown> | null;
  decided_at: string | null;
  created_at: string;
};
export type ApprovalDecision = "approve" | "reject" | "edit";
export type ApprovalDecisionResult = {
  approval: Approval;
  feedback: Record<string, unknown> | null;
};
export type Run = {
  id: string;
  project_id: string;
  idea: string;
  autonomy_level: AutonomyLevel;
  budget_usd: string;
  status: string;
  created_at: string;
};
export type RunEvent = {
  id: string;
  run_id: string;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};
export type QuestionOption = { id: string; label: string };
export type RunQuestion = {
  id: string;
  prompt: string;
  type: "single_choice" | "multi_choice" | "free_text";
  options: QuestionOption[];
  allow_decide_for_me: boolean;
};
export type Plan = {
  product_brief: string;
  tech_stack: string[];
  milestones: { id: string; title: string; description: string }[];
  tasks: { id: string; title: string; description: string; depends_on: string[] }[];
};

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_URL}/api/v1${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    let detail: unknown;
    if (typeof body === "object" && body !== null && "detail" in body) {
      detail = body.detail;
    }
    if (typeof detail === "string") throw new Error(detail);
    if (Array.isArray(detail)) {
      const messages = detail.flatMap((item: unknown): string[] => {
        if (typeof item !== "object" || item === null || !("msg" in item)) return [];
        const message = typeof item.msg === "string" ? item.msg : "";
        const location = "loc" in item && Array.isArray(item.loc)
          ? item.loc.filter(
              (part: unknown): part is string | number =>
                typeof part === "string" || typeof part === "number",
            )
          : [];
        return [location.length > 0 ? `${location.join(" ")}: ${message}` : message].filter(Boolean);
      });
      if (messages.length > 0) throw new Error(messages.join("; "));
    }
    throw new Error("Request failed.");
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  signup: (email: string, password: string) => request<User>("/auth/signup", { method: "POST", body: JSON.stringify({ email, password }) }),
  login: (email: string, password: string) => request<User>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: () => request<User>("/me"),
  providerKeys: () => request<ProviderKey[]>("/provider-keys"),
  addProviderKey: (provider: Provider, label: string, api_key: string) => request<ProviderKey>("/provider-keys", { method: "POST", body: JSON.stringify({ provider, label, api_key }) }),
  testProviderKey: (id: string) => request<ProviderTest>(`/provider-keys/${id}/test`, { method: "POST" }),
  removeProviderKey: (id: string) => request<void>(`/provider-keys/${id}`, { method: "DELETE" }),
  createRun: (idea: string, autonomy_level: AutonomyLevel, budget_usd: number) => request<Run>("/runs", { method: "POST", body: JSON.stringify({ idea, autonomy_level, budget_usd }) }),
  runs: () => request<Run[]>("/runs"),
  run: (id: string) => request<Run>(`/runs/${id}`),
  runEvents: (id: string, afterSeq: number) => request<RunEvent[]>(`/runs/${id}/events?after_seq=${afterSeq}`),
  submitRunAnswers: (id: string, answers: Record<string, unknown>) => request<Run>(`/runs/${id}/answers`, { method: "POST", body: JSON.stringify({ answers }) }),
  updateRunPlan: (id: string, plan: Plan) => request<Plan>(`/runs/${id}/plan`, { method: "PUT", body: JSON.stringify({ plan }) }),
  approveRunPlan: (id: string) => request<Run>(`/runs/${id}/plan/approve`, { method: "POST" }),
  runApprovals: (id: string) => request<Approval[]>(`/runs/${id}/approvals`),
  decideApproval: (id: string, decision: ApprovalDecision, edited_payload?: Record<string, unknown>) => request<ApprovalDecisionResult>(`/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ decision, edited_payload }) }),
  updateRunAutonomy: (id: string, autonomy_level: AutonomyLevel) => request<Run>(`/runs/${id}/autonomy`, { method: "PATCH", body: JSON.stringify({ autonomy_level }) }),
};
