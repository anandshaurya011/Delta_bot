"use client";
import { Fragment, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { Chip } from "@/components/Ui";
import {
  type AiSettings,
  type AiSignal,
  validateCooldownMinutes,
  validateSymbolAllowlist,
  formatGuardrailSummary,
} from "@/lib/aiTrading";

const ALL_SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"];
const MODELS = ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"];

export default function AiTradingPage() {
  const toast = useToast();
  const [settings, setSettings] = useState<AiSettings | null>(null);
  const [signals, setSignals] = useState<AiSignal[]>([]);
  const [expanded, setExpanded] = useState<number | null>(null);

  async function load() {
    try {
      const [s, sigs] = await Promise.all([
        api<AiSettings>("GET", "/ai/settings"),
        api<AiSignal[]>("GET", "/ai/signals?limit=50"),
      ]);
      setSettings(s);
      setSignals(sigs);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to load AI trading settings", "err");
    }
  }

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save(patch: Partial<AiSettings>) {
    if (!settings) return;
    try {
      const updated = await api<AiSettings>("PUT", "/ai/settings", patch);
      setSettings(updated);
      toast("Saved", "ok");
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to save", "err");
    }
  }

  async function regenerate() {
    if (!confirm("Regenerate the webhook URL? The old URL will stop working immediately.")) return;
    try {
      const { webhook_token } = await api<{ webhook_token: string }>("POST", "/ai/webhook/regenerate");
      setSettings((s) => (s ? { ...s, webhook_token } : s));
      toast("Webhook URL regenerated", "ok");
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to regenerate webhook", "err");
    }
  }

  function toggleGuardrail(name: string, enabled: boolean) {
    if (!settings) return;
    const current = settings.guardrails_json[name] ?? { enabled: false, value: name === "symbol_allowlist" ? [] : 0 };
    save({ guardrails_json: { ...settings.guardrails_json, [name]: { ...current, enabled } } });
  }

  function setGuardrailValue(name: string, value: number | string[]) {
    if (!settings) return;
    const current = settings.guardrails_json[name] ?? { enabled: false, value };
    save({ guardrails_json: { ...settings.guardrails_json, [name]: { ...current, value } } });
  }

  if (!settings) return <div>Loading…</div>;

  const webhookUrl = typeof window !== "undefined" ? `${window.location.origin}/api/webhook/tv/${settings.webhook_token}` : "";
  const allowlist = (settings.guardrails_json.symbol_allowlist?.value as string[] | undefined) ?? [];
  const cooldownError = validateCooldownMinutes(settings.cooldown_minutes);
  const allowlistError = settings.guardrails_json.symbol_allowlist?.enabled ? validateSymbolAllowlist(allowlist) : null;

  return (
    <div>
      <section>
        <h2>AI Trading</h2>
        <p className="muted">{formatGuardrailSummary(settings)}</p>

        <div className="autoRow" style={{ alignItems: "center", gap: 10 }}>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={settings.enabled} onChange={(e) => save({ enabled: e.target.checked })} />
            Enable AI trading
          </label>
          <label>
            Cooldown (minutes)
            <input
              type="number"
              min={1}
              style={{ width: 70 }}
              value={settings.cooldown_minutes}
              onChange={(e) => save({ cooldown_minutes: +e.target.value })}
            />
          </label>
        </div>
        {cooldownError && <p className="err">{cooldownError}</p>}

        <div className="autoRow" style={{ marginTop: 10 }}>
          <code style={{ flex: 1, wordBreak: "break-all" }}>{webhookUrl}</code>
          <button type="button" className="btn ghost sm" onClick={() => navigator.clipboard.writeText(webhookUrl)}>
            Copy
          </button>
          <button type="button" className="btn ghost sm" onClick={regenerate}>
            Regenerate
          </button>
        </div>

        <h3 style={{ marginTop: 20 }}>Guardrails</h3>
        <div className="autoRow" style={{ flexDirection: "column", gap: 10 }}>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={!!settings.guardrails_json.max_position_size?.enabled}
              onChange={(e) => toggleGuardrail("max_position_size", e.target.checked)}
            />
            Max position size
            <input
              type="number"
              style={{ width: 80 }}
              value={(settings.guardrails_json.max_position_size?.value as number) ?? 0}
              onChange={(e) => setGuardrailValue("max_position_size", +e.target.value)}
            />
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={!!settings.guardrails_json.max_trades_per_day?.enabled}
              onChange={(e) => toggleGuardrail("max_trades_per_day", e.target.checked)}
            />
            Max trades per day
            <input
              type="number"
              style={{ width: 80 }}
              value={(settings.guardrails_json.max_trades_per_day?.value as number) ?? 0}
              onChange={(e) => setGuardrailValue("max_trades_per_day", +e.target.value)}
            />
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={!!settings.guardrails_json.max_daily_loss?.enabled}
              onChange={(e) => toggleGuardrail("max_daily_loss", e.target.checked)}
            />
            Max daily loss (USD)
            <input
              type="number"
              style={{ width: 80 }}
              value={(settings.guardrails_json.max_daily_loss?.value as number) ?? 0}
              onChange={(e) => setGuardrailValue("max_daily_loss", +e.target.value)}
            />
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={!!settings.guardrails_json.symbol_allowlist?.enabled}
              onChange={(e) => toggleGuardrail("symbol_allowlist", e.target.checked)}
            />
            Symbol allowlist
          </label>
          <div className="chips">
            {ALL_SYMBOLS.map((s) => (
              <Chip
                key={s}
                on={allowlist.includes(s)}
                onClick={() => setGuardrailValue("symbol_allowlist", allowlist.includes(s) ? allowlist.filter((x) => x !== s) : [...allowlist, s])}
              >
                {s}
              </Chip>
            ))}
          </div>
          {allowlistError && <p className="err">{allowlistError}</p>}
        </div>

        <h3 style={{ marginTop: 20 }}>Agent instructions</h3>
        {(["coordinator", "data", "executer"] as const).map((agent) => (
          <div key={agent} style={{ marginBottom: 14 }}>
            <label>
              {agent} model
              <select
                value={settings[`${agent}_model` as const]}
                onChange={(e) => save({ [`${agent}_model`]: e.target.value } as Partial<AiSettings>)}
              >
                {MODELS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
            <textarea
              rows={3}
              style={{ width: "100%" }}
              placeholder={`Extra instructions for the ${agent} agent`}
              defaultValue={settings[`${agent}_prompt` as const]}
              onBlur={(e) => save({ [`${agent}_prompt`]: e.target.value } as Partial<AiSettings>)}
            />
          </div>
        ))}

        <h3 style={{ marginTop: 20 }}>Decision log</h3>
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Symbol</th>
              <th>TF</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {signals.map((s) => (
              <Fragment key={s.id}>
                <tr onClick={() => setExpanded(expanded === s.id ? null : s.id)} style={{ cursor: "pointer" }}>
                  <td>{new Date(s.ts * 1000).toLocaleString()}</td>
                  <td>{s.symbol}</td>
                  <td>{s.timeframe}</td>
                  <td>{s.status}</td>
                </tr>
                {expanded === s.id && (
                  <tr>
                    <td colSpan={4}>
                      <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(s.result_json, null, 2)}</pre>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
