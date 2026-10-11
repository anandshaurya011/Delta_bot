"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { num } from "@/lib/format";
import { useToast } from "./Toast";

// Ported from the legacy UI lines 391-428 (openProtect / #prApply handler)
// and the <dialog id="protect"> markup at lines 273-284.

export type ProtectCtx =
  | { kind: "position"; symbol: string; long: boolean; size: number; entry: number; mark: number }
  | { kind: "order"; symbol: string; orderId: number | string };

export function ProtectDialog({
  ctx,
  onClose,
  onDone,
}: {
  ctx: ProtectCtx | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const [sl, setSl] = useState("");
  const [tp, setTp] = useState("");
  const [trail, setTrail] = useState("");
  const [size, setSize] = useState("");
  const [msg, setMsg] = useState<{ text: string; cls: string }>({ text: "", cls: "" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (ctx) {
      setSl("");
      setTp("");
      setTrail("");
      setSize(ctx.kind === "position" ? String(ctx.size) : "");
      setMsg({ text: "", cls: "" });
      requestAnimationFrame(() => dialogRef.current?.showModal());
    } else {
      dialogRef.current?.close();
    }
  }, [ctx]);

  function close() {
    dialogRef.current?.close();
    onClose();
  }

  async function apply() {
    if (!ctx) return;
    const v = (s: string) => (s === "" ? null : +s);
    const slv = v(sl), tpv = v(tp), trv = v(trail);
    const fail = (t: string) => setMsg({ text: t, cls: "r" });
    if (slv == null && tpv == null && trv == null) return fail("Fill at least one of SL, TP, trailing.");
    if ([slv, tpv, trv].some((x) => x != null && !(x > 0))) return fail("Values must be positive.");

    setBusy(true);
    setMsg({ text: "Working…", cls: "" });
    const done: string[] = [];
    const errs: string[] = [];
    const step = async (label: string, fn: () => Promise<any>) => {
      try {
        await fn();
        done.push(label);
      } catch (err) {
        errs.push(label + ": " + (err instanceof ApiError ? err.message : String(err)));
      }
    };

    if (ctx.kind === "position") {
      const sz = Math.trunc(+size);
      if (!(sz > 0)) {
        setBusy(false);
        return fail("Enter a size.");
      }
      const side = ctx.long ? "sell" : "buy";
      if (slv != null) await step("stop-loss", () => api("POST", "/orders/stop-loss", { symbol: ctx.symbol, side, size: sz, stop_price: slv }));
      if (tpv != null) await step("take-profit", () => api("POST", "/orders/take-profit", { symbol: ctx.symbol, side, size: sz, stop_price: tpv }));
      if (trv != null) await step("trailing stop", () => api("POST", "/orders/trailing-stop", { symbol: ctx.symbol, side, size: sz, trail_amount: trv }));
    } else {
      await step("bracket update", () => api("PUT", "/orders/bracket", { symbol: ctx.symbol, stop_loss: slv, take_profit: tpv, trail_amount: trv }));
    }

    setBusy(false);
    if (errs.length) {
      fail((done.length ? "Placed: " + done.join(", ") + ". " : "") + "Failed - " + errs.join(" | "));
    } else {
      setMsg({ text: "Done: " + done.join(", "), cls: "g" });
      toast("Placed: " + done.join(", "));
      setTimeout(() => close(), 900);
    }
    onDone();
  }

  const title = !ctx
    ? ""
    : ctx.kind === "position"
    ? `Protect ${ctx.symbol} position`
    : `SL / TP for order ${ctx.orderId} (${ctx.symbol})`;
  const info = !ctx
    ? ""
    : ctx.kind === "position"
    ? `${ctx.long ? "LONG" : "SHORT"} ${ctx.size} @ ${num(ctx.entry)} (mark ${num(ctx.mark)}). Stop-loss goes ${
        ctx.long ? "below" : "above"
      } the mark, take-profit ${ctx.long ? "above" : "below"}. Fill any of the three; each becomes its own closing order.`
    : "Edits the SL / TP / trailing distance on the bracket attached to this symbol's order. Fill any of the three.";

  return (
    <dialog ref={dialogRef} style={{ maxWidth: 420 }} onCancel={(e) => { e.preventDefault(); close(); }}>
      {ctx && (
        <div style={{ padding: 4 }}>
          <h2 style={{ color: "var(--text)" }}>{title}</h2>
          <p className="muted" style={{ marginTop: 0 }}>{info}</p>
          <div className="row" style={{ flexDirection: "column", alignItems: "stretch" }}>
            <label>
              Stop-loss price
              <input type="number" step="any" value={sl} onChange={(e) => setSl(e.target.value)} />
            </label>
            <label>
              Take-profit price
              <input type="number" step="any" value={tp} onChange={(e) => setTp(e.target.value)} />
            </label>
            <label>
              Trailing stop distance (price units)
              <input type="number" step="any" min={0} value={trail} onChange={(e) => setTrail(e.target.value)} />
            </label>
            {ctx.kind === "position" && (
              <label>
                Size to protect
                <input type="number" min={1} step={1} value={size} onChange={(e) => setSize(e.target.value)} />
              </label>
            )}
          </div>
          <div style={{ margin: "10px 0", fontWeight: 600 }} className={msg.cls}>{msg.text}</div>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="btn ghost" onClick={close}>Close</button>
            <button type="button" className="btn" disabled={busy} onClick={apply}>Apply</button>
          </div>
        </div>
      )}
    </dialog>
  );
}
