export type DeltaSettings = {
  is_set: boolean;
  env: "india_prod" | "india_testnet" | null;
  api_key_masked: string | null;
};

export function validateCredsForm(f: { apiKey: string; apiSecret: string }): string | null {
  if (f.apiKey.trim().length < 4) return "Enter your Delta API key.";
  if (f.apiSecret.trim().length < 4) return "Enter your Delta API secret.";
  return null;
}
