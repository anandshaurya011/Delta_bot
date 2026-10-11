// frontend/lib/aiTrading.test.ts
import { expect, it } from "vitest";
import { validateCooldownMinutes, validateSymbolAllowlist, formatGuardrailSummary, type AiSettings } from "./aiTrading";

it("validates cooldown minutes", () => {
  expect(validateCooldownMinutes(0)).toMatch(/at least/i);
  expect(validateCooldownMinutes(-5)).toMatch(/at least/i);
  expect(validateCooldownMinutes(30)).toBeNull();
});

it("validates symbol allowlist", () => {
  expect(validateSymbolAllowlist([])).toMatch(/at least one symbol/i);
  expect(validateSymbolAllowlist(["BTCUSD"])).toBeNull();
});

const baseSettings: AiSettings = {
  user_id: "u1",
  webhook_token: "tok",
  enabled: true,
  cooldown_minutes: 30,
  coordinator_model: "gpt-4.1",
  data_model: "gpt-4.1-mini",
  executer_model: "gpt-4.1-mini",
  coordinator_prompt: "",
  data_prompt: "",
  executer_prompt: "",
  guardrails_json: {},
  updated_at: 0,
};

it("summarizes zero enabled guardrails", () => {
  expect(formatGuardrailSummary(baseSettings)).toBe("No guardrails enabled");
});

it("summarizes enabled guardrail count", () => {
  const settings: AiSettings = {
    ...baseSettings,
    guardrails_json: {
      max_position_size: { enabled: true, value: 50 },
      symbol_allowlist: { enabled: false, value: [] },
      max_trades_per_day: { enabled: true, value: 10 },
    },
  };
  expect(formatGuardrailSummary(settings)).toBe("2 guardrails active");
});
