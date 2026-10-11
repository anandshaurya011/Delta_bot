export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export function errorMessage(status: number, data: any): string {
  const d = data?.delta_error ? JSON.stringify(data.delta_error) : typeof data?.detail === "string" ? data.detail : JSON.stringify(data?.detail ?? data);
  return `${status}: ${d}`;
}
export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, { method, headers: body !== undefined ? { "Content-Type": "application/json" } : undefined, body: body !== undefined ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { window.location.assign("/sign-in"); throw new ApiError(401, "Session expired"); }
  if (res.status === 412) { window.location.assign("/settings?missing=1"); throw new ApiError(412, "Add your Delta API keys in Settings"); }
  if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, data));
  return data as T;
}
