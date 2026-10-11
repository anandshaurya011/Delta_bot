"use client";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { useConfirm } from "@/components/ConfirmDialog";
import { type DeltaSettings, validateCredsForm } from "@/lib/settings";

const ENV_LABELS: Record<string, string> = {
  india_prod: "India production",
  india_testnet: "India testnet",
};

export default function SettingsPage() {
  return (
    <Suspense fallback={null}>
      <SettingsContent />
    </Suspense>
  );
}

function SettingsContent() {
  const searchParams = useSearchParams();
  const missing = searchParams.get("missing") === "1";
  const toast = useToast();
  const confirm = useConfirm();
  const qc = useQueryClient();

  const { data } = useQuery({
    queryKey: ["delta-settings"],
    queryFn: () => api<DeltaSettings>("GET", "/settings/delta"),
  });

  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [env, setEnv] = useState<"india_prod" | "india_testnet">("india_prod");
  const [saving, setSaving] = useState(false);

  async function onSave() {
    const err = validateCredsForm({ apiKey, apiSecret });
    if (err) {
      toast(err, "err");
      return;
    }
    setSaving(true);
    try {
      await api<DeltaSettings>("PUT", "/settings/delta", {
        api_key: apiKey.trim(),
        api_secret: apiSecret.trim(),
        env,
      });
      toast("Keys verified and saved", "ok");
      setApiSecret("");
      await qc.invalidateQueries({ queryKey: ["delta-settings"] });
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Failed to save keys";
      toast(msg, "err");
    } finally {
      setSaving(false);
    }
  }

  async function onRemove() {
    const ok = await confirm("Remove your Delta API keys?");
    if (!ok) return;
    try {
      await api<DeltaSettings>("DELETE", "/settings/delta");
      toast("Keys removed", "ok");
      await qc.invalidateQueries({ queryKey: ["delta-settings"] });
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Failed to remove keys";
      toast(msg, "err");
    }
  }

  return (
    <div>
      <h2>Settings</h2>
      {missing && (
        <section style={{ borderColor: "var(--warn)" }}>
          <span className="w">Add your Delta API keys to continue.</span>
        </section>
      )}
      <section>
        <h2>Delta API connection</h2>
        <p>
          {data?.is_set
            ? `Connected: key ${data.api_key_masked} on ${ENV_LABELS[data.env ?? ""] ?? data.env}`
            : "Not set"}
        </p>
        {data?.is_set && (
          <button className="btn ghost" onClick={onRemove}>
            Remove keys
          </button>
        )}
      </section>
      <section>
        <h2>{data?.is_set ? "Update keys" : "Add your keys"}</h2>
        <div className="row">
          <label>
            API key
            <input
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="off"
            />
          </label>
          <label>
            API secret
            <input
              type="password"
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
              autoComplete="new-password"
            />
          </label>
          <label>
            Environment
            <select value={env} onChange={(e) => setEnv(e.target.value as "india_prod" | "india_testnet")}>
              <option value="india_prod">India production</option>
              <option value="india_testnet">India testnet</option>
            </select>
          </label>
          <button className="btn" onClick={onSave} disabled={saving}>
            Save
          </button>
        </div>
        <p className="muted">Your secret is encrypted on the server and never shown again.</p>
      </section>
    </div>
  );
}
