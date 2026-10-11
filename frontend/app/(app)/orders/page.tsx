"use client";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { num, signClass } from "@/lib/format";
import { useSymbol } from "@/lib/symbol";
import { useToast } from "@/components/Toast";
import { useConfirm } from "@/components/ConfirmDialog";
import { Stat } from "@/components/Ui";
import { ProtectDialog, type ProtectCtx } from "@/components/ProtectDialog";
import { kindOf, pnlOf, type OrderHistoryEntry } from "@/lib/history";

// Ported from the legacy UI lines 149-184 (markup, including the "Last
// closed trade" / "Order history" sections) and 437-463, 548-594 (loadHistoryOrders,
// loadOrders, cancelOne, cancelAll/cancelAllSym, pCancelTrail, pGo / protect form).

type Position = {
  symbol: string;
  side: "long" | "short";
  size: number;
  entry_price: number;
  mark_price: number;
  unrealized_pnl: number;
  realized_pnl: number;
};

type Order = {
  id: number | string;
  product_symbol: string;
  side: string;
  order_type: string;
  size: number;
  unfilled_size: number;
  limit_price?: number | string | null;
  stop_price?: number | string | null;
  trail_amount?: number | string | null;
  bracket_stop_loss_price?: number | string | null;
  bracket_take_profit_price?: number | string | null;
  state: string;
};

const PLACE_TYPES = [
  { value: "stop-loss", label: "Stop-loss", priceLabel: "Stop price" },
  { value: "take-profit", label: "Take-profit", priceLabel: "Take-profit price" },
  { value: "trailing-stop", label: "Trailing stop", priceLabel: "Trail amount" },
] as const;

export default function OrdersPage() {
  const [symbol] = useSymbol();
  const toast = useToast();
  const confirm = useConfirm();
  const qc = useQueryClient();

  const [protectCtx, setProtectCtx] = useState<ProtectCtx | null>(null);

  const [pType, setPType] = useState<(typeof PLACE_TYPES)[number]["value"]>("stop-loss");
  const [pSide, setPSide] = useState<"sell" | "buy">("sell");
  const [pSize, setPSize] = useState("1");
  const [pPrice, setPPrice] = useState("");
  const [pMsg, setPMsg] = useState<{ text: string; cls: string }>({ text: "", cls: "" });
  const [histSym, setHistSym] = useState(false);
  const [histFilled, setHistFilled] = useState(true);

  const positionsQ = useQuery({
    queryKey: ["positions"],
    queryFn: () => api<Position[]>("GET", "/positions"),
    refetchInterval: 5000,
  });
  const ordersQ = useQuery({
    queryKey: ["orders"],
    queryFn: () => api<Order[]>("GET", "/orders"),
    refetchInterval: 5000,
  });
  const histOrdersQ = useQuery({
    queryKey: ["orders-history", histSym, symbol],
    queryFn: () =>
      api<OrderHistoryEntry[]>("GET", `/orders/history?limit=100${histSym ? `&symbol=${encodeURIComponent(symbol)}` : ""}`),
  });

  function invalidateAll() {
    qc.invalidateQueries({ queryKey: ["positions"] });
    qc.invalidateQueries({ queryKey: ["orders"] });
    qc.invalidateQueries({ queryKey: ["orders-history"] });
    qc.invalidateQueries({ queryKey: ["history"] });
  }

  const cancelOne = useMutation({
    mutationFn: (o: Order) => api("DELETE", `/orders/${encodeURIComponent(o.product_symbol)}/${o.id}`),
    onSuccess: () => {
      toast("Order cancelled");
      invalidateAll();
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Failed to cancel order", "err"),
  });
  const cancelAllSym = useMutation({
    mutationFn: () => api("DELETE", `/orders?symbol=${encodeURIComponent(symbol)}`),
    onSuccess: () => {
      toast("Cancelled");
      invalidateAll();
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Failed to cancel orders", "err"),
  });
  const cancelAll = useMutation({
    mutationFn: () => api("DELETE", "/orders"),
    onSuccess: () => {
      toast("Cancelled");
      invalidateAll();
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Failed to cancel orders", "err"),
  });
  const cancelTrail = useMutation({
    mutationFn: () => api<any[]>("DELETE", `/orders/${encodeURIComponent(symbol)}/trailing-stop`),
    onSuccess: (r) => {
      toast(r.length ? "Cancelled" : "None open");
      invalidateAll();
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Failed to cancel trailing stop", "err"),
  });
  const place = useMutation({
    mutationFn: (body: Record<string, unknown>) => api("POST", `/orders/${pType}`, body),
    onSuccess: () => {
      setPMsg({ text: pType + " placed.", cls: "g" });
      toast(pType + " placed");
      invalidateAll();
    },
    onError: (e) => {
      const message = e instanceof ApiError ? e.message : "Failed";
      setPMsg({ text: "Failed: " + message, cls: "r" });
    },
  });

  const pos = positionsQ.data ?? [];
  const orders = ordersQ.data ?? [];
  const currentPos = useMemo(() => pos.find((p) => p.symbol === symbol), [pos, symbol]);

  const pHint = currentPos
    ? `Open position: ${currentPos.side.toUpperCase()} ${Math.abs(currentPos.size)} ${currentPos.symbol} @ ${num(
        currentPos.entry_price,
      )} (mark ${num(currentPos.mark_price)}). ` +
      (currentPos.side === "long" ? "Stop-loss goes BELOW the mark, take-profit ABOVE." : "Stop-loss goes ABOVE the mark, take-profit BELOW.")
    : `No open ${symbol} position - stop-loss / take-profit orders need one.`;

  const priceLabel = PLACE_TYPES.find((t) => t.value === pType)?.priceLabel ?? "Stop price";

  const when = (o: OrderHistoryEntry) => new Date(o.updated_at || o.created_at || 0).toLocaleString();
  const histList = useMemo(() => {
    const list = histOrdersQ.data ?? [];
    return histFilled ? list.filter((o) => o.state !== "cancelled") : list;
  }, [histOrdersQ.data, histFilled]);
  const lastTrade = useMemo(() => histList.find((o) => pnlOf(o) != null) ?? null, [histList]);
  const histTotal = useMemo(() => histList.reduce((a, o) => a + (pnlOf(o) ?? 0), 0), [histList]);
  const histHasPnl = histList.some((o) => pnlOf(o) != null);

  async function onCancelOne(o: Order) {
    if (!(await confirm(`Cancel order ${o.id} (${o.product_symbol})?`))) return;
    cancelOne.mutate(o);
  }
  async function onCancelAllSym() {
    if (!(await confirm(`Cancel ALL open orders for ${symbol}?`))) return;
    cancelAllSym.mutate();
  }
  async function onCancelAll() {
    if (!(await confirm("Cancel ALL open orders on the account?"))) return;
    cancelAll.mutate();
  }
  async function onCancelTrail() {
    if (!(await confirm(`Cancel trailing stop(s) on ${symbol}?`))) return;
    cancelTrail.mutate();
  }
  async function onPlace() {
    const size = Math.trunc(+pSize);
    const p = +pPrice;
    if (!(size > 0) || !(p > 0)) {
      setPMsg({ text: `Enter a size and a ${priceLabel.toLowerCase()} first.`, cls: "r" });
      return;
    }
    if (!(await confirm(`${pType} on ${symbol}: ${pSide} ${size} @ ${p}`))) return;
    setPMsg({ text: "", cls: "" });
    const body = pType === "trailing-stop" ? { symbol, side: pSide, size, trail_amount: p } : { symbol, side: pSide, size, stop_price: p };
    place.mutate(body);
  }

  return (
    <div>
      <section>
        <h2>Open positions &amp; P&amp;L</h2>
        <div className="tablewrap">
          {pos.length === 0 ? (
            <div className="muted">No open positions.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Size</th>
                  <th>Entry</th>
                  <th>Mark</th>
                  <th>Unrealized P&amp;L</th>
                  <th>Realized P&amp;L</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {pos.map((p) => (
                  <tr key={p.symbol}>
                    <td>{p.symbol}</td>
                    <td className={p.side === "long" ? "g" : "r"}>{p.side.toUpperCase()}</td>
                    <td>{num(Math.abs(p.size), 0)}</td>
                    <td>{num(p.entry_price)}</td>
                    <td>{num(p.mark_price)}</td>
                    <td className={signClass(p.unrealized_pnl)}>{(p.unrealized_pnl > 0 ? "+" : "") + num(p.unrealized_pnl, 4)}</td>
                    <td>{num(p.realized_pnl, 4)}</td>
                    <td>
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() =>
                          setProtectCtx({
                            kind: "position",
                            symbol: p.symbol,
                            long: p.side === "long",
                            size: Math.abs(p.size),
                            entry: p.entry_price,
                            mark: p.mark_price,
                          })
                        }
                      >
                        SL / TP
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section>
        <h2>Open orders</h2>
        <div className="row" style={{ marginBottom: 10 }}>
          <button type="button" className="btn ghost sm" onClick={() => ordersQ.refetch()}>
            Refresh
          </button>
          <button type="button" className="btn ghost sm" onClick={onCancelAllSym}>
            Cancel all (this symbol)
          </button>
          <button type="button" className="btn ghost sm" onClick={onCancelAll}>
            Cancel all (everything)
          </button>
        </div>
        <div className="tablewrap">
          {orders.length === 0 ? (
            <div className="muted">Nothing to show.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Type</th>
                  <th>Size</th>
                  <th>Unfilled</th>
                  <th>Limit</th>
                  <th>Stop / trail</th>
                  <th>State</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.id}>
                    <td>{o.id}</td>
                    <td>{o.product_symbol}</td>
                    <td className={o.side === "buy" ? "g" : "r"}>{(o.side || "").toUpperCase()}</td>
                    <td>{o.order_type}</td>
                    <td>{o.size}</td>
                    <td>{o.unfilled_size}</td>
                    <td>{o.limit_price || "–"}</td>
                    <td>
                      {o.trail_amount
                        ? "trail " + o.trail_amount
                        : o.stop_price
                        ? "stop " + o.stop_price
                        : o.bracket_stop_loss_price || o.bracket_take_profit_price
                        ? `SL ${o.bracket_stop_loss_price || "–"} / TP ${o.bracket_take_profit_price || "–"}`
                        : "–"}
                    </td>
                    <td>{o.state}</td>
                    <td>
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() => setProtectCtx({ kind: "order", symbol: o.product_symbol, orderId: o.id })}
                      >
                        SL / TP
                      </button>{" "}
                      <button type="button" className="btn ghost sm" onClick={() => onCancelOne(o)}>
                        Cancel
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section>
        <h2>Last closed trade</h2>
        {lastTrade ? (
          (() => {
            const m = lastTrade.meta_data || {};
            const pnl = +m.pnl;
            return (
              <div className="stats" style={{ margin: 0 }}>
                <Stat label="Symbol" value={lastTrade.product_symbol ?? "–"} />
                <Stat label="Closed" value={when(lastTrade)} />
                <Stat
                  label="Direction"
                  value={lastTrade.side === "sell" ? "LONG" : "SHORT"}
                  cls={lastTrade.side === "sell" ? "g" : "r"}
                />
                <Stat label="Size" value={lastTrade.size ?? "–"} />
                <Stat label="Entry → Exit" value={`${num(m.entry_price)} → ${num(m.avg_exit_price)}`} />
                <Stat label="Closed by" value={kindOf(lastTrade)} />
                <Stat label="P&L" value={(pnl > 0 ? "+" : "") + num(pnl, 4)} cls={signClass(pnl)} />
                <Stat label="Net P&L" value={m.net_pnl == null ? "–" : num(m.net_pnl, 4)} cls={signClass(+m.net_pnl)} />
                <Stat label="ROE" value={m.roe == null ? "–" : num(m.roe, 2) + "%"} cls={signClass(+m.roe)} />
              </div>
            );
          })()
        ) : (
          <div className="muted">No closed trades yet.</div>
        )}
      </section>

      <section>
        <h2>
          Order history{" "}
          <span className="muted">{histHasPnl ? `· realized in list: ${histTotal > 0 ? "+" : ""}${num(histTotal, 4)}` : ""}</span>
        </h2>
        <div className="row" style={{ marginBottom: 10 }}>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={histSym} onChange={(e) => setHistSym(e.target.checked)} /> this symbol only
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={histFilled} onChange={(e) => setHistFilled(e.target.checked)} /> hide cancelled
          </label>
          <button type="button" className="btn ghost sm" onClick={() => histOrdersQ.refetch()}>
            Refresh
          </button>
        </div>
        <div className="tablewrap">
          {histList.length === 0 ? (
            <div className="muted">Nothing to show.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Type</th>
                  <th>Size</th>
                  <th>Avg price</th>
                  <th>Entry → Exit</th>
                  <th>P&amp;L</th>
                  <th>Net P&amp;L</th>
                  <th>Fee</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {histList.map((o) => {
                  const pnl = pnlOf(o);
                  const m = o.meta_data || {};
                  return (
                    <tr key={o.id}>
                      <td>{when(o)}</td>
                      <td>{o.product_symbol}</td>
                      <td className={o.side === "buy" ? "g" : "r"}>{(o.side || "").toUpperCase()}</td>
                      <td>{kindOf(o)}</td>
                      <td>{o.size}</td>
                      <td>{o.average_fill_price ? num(o.average_fill_price) : "–"}</td>
                      <td>{pnl != null ? `${num(m.entry_price)} → ${num(m.avg_exit_price)}` : "–"}</td>
                      <td className={pnl != null ? signClass(pnl) : "d"}>{pnl != null ? (pnl > 0 ? "+" : "") + num(pnl, 4) : "–"}</td>
                      <td className={m.net_pnl != null ? signClass(+m.net_pnl) : "d"}>{m.net_pnl != null ? num(m.net_pnl, 4) : "–"}</td>
                      <td>{num(o.paid_commission, 4)}</td>
                      <td className={o.state === "closed" ? "g" : "d"}>
                        {o.state + (o.cancellation_reason ? ` (${o.cancellation_reason.replace(/_/g, " ")})` : "")}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section>
        <h2>Protect a position</h2>
        <div className="row">
          <label>
            Type
            <select value={pType} onChange={(e) => setPType(e.target.value as typeof pType)}>
              {PLACE_TYPES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Closing side
            <select value={pSide} onChange={(e) => setPSide(e.target.value as "sell" | "buy")}>
              <option value="sell">sell (protects long)</option>
              <option value="buy">buy (protects short)</option>
            </select>
          </label>
          <label>
            Size
            <input type="number" min={1} step={1} style={{ width: 90 }} value={pSize} onChange={(e) => setPSize(e.target.value)} />
          </label>
          <label>
            <span>{priceLabel}</span>
            <input type="number" step="any" style={{ width: 130 }} value={pPrice} onChange={(e) => setPPrice(e.target.value)} />
          </label>
          <button type="button" className="btn" disabled={place.isPending} onClick={onPlace}>
            Place
          </button>
          <button type="button" className="btn ghost" onClick={onCancelTrail}>
            Cancel trailing stop
          </button>
        </div>
        <div className="muted" style={{ marginTop: 10 }}>{pHint}</div>
        <div style={{ marginTop: 8, fontWeight: 600 }} className={pMsg.cls}>{pMsg.text}</div>
      </section>

      <ProtectDialog ctx={protectCtx} onClose={() => setProtectCtx(null)} onDone={invalidateAll} />
    </div>
  );
}
