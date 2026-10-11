export type GuardrailConfig = { enabled: boolean; value: number | string[] };

export type AiSettings = {
  user_id: string;
  webhook_token: string;
  enabled: boolean;
  cooldown_minutes: number;
  coordinator_model: string;
  data_model: string;
  executer_model: string;
  coordinator_prompt: string;
  data_prompt: string;
  executer_prompt: string;
  guardrails_json: Record<string, GuardrailConfig>;
  updated_at: number;
};

export type AiSignal = {
  id: number;
  ts: number;
  symbol: string;
  timeframe: string;
  action: string;
  note: string | null;
  status: "received" | "duplicate_skipped" | "cooldown_skipped" | "processing" | "done" | "error";
  result_json: Record<string, any> | null;
};

export function validateCooldownMinutes(v: number): string | null {
  if (!Number.isFinite(v) || v < 1) return "Cooldown must be at least 1 minute.";
  return null;
}

export function validateSymbolAllowlist(v: string[]): string | null {
  if (v.length === 0) return "Pick at least one symbol for the allowlist, or disable it.";
  return null;
}

export function formatGuardrailSummary(settings: AiSettings): string {
  const count = Object.values(settings.guardrails_json).filter((g) => g.enabled).length;
  return count === 0 ? "No guardrails enabled" : `${count} guardrail${count === 1 ? "" : "s"} active`;
}
