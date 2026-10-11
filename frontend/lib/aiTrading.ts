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
  paper_trading: boolean;
  updated_at: number;
};

export type PaperTrade = {
  id: number;
  user_id: string;
  signal_id: number | null;
  ts: number;
  symbol: string;
  side: "buy" | "sell";
  size: number;
  entry_price: number;
  status: "open" | "closed";
  exit_price: number | null;
  closed_ts: number | null;
  pnl: number | null;
};

export type PaperPnlSummary = {
  realized_pnl: number;
  closed_trades: number;
  open_trades: number;
  win_rate: number | null;
};

export function formatPaperPnlSummary(summary: PaperPnlSummary): string {
  const pnl = summary.realized_pnl.toFixed(2);
  const sign = summary.realized_pnl > 0 ? "+" : "";
  const winRate = summary.win_rate == null ? "—" : `${(summary.win_rate * 100).toFixed(0)}%`;
  return `${sign}${pnl} realized over ${summary.closed_trades} closed trade${summary.closed_trades === 1 ? "" : "s"} (${winRate} win rate), ${summary.open_trades} open`;
}

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
