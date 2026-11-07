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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_URL}/api/v1${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const message = typeof body === "object" && body !== null && "detail" in body
      ? String(body.detail)
      : "Request failed.";
    throw new Error(message);
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
};
