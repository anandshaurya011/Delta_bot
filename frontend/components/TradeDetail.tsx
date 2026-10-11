"use client";
import { useEffect, useRef, type ReactNode } from "react";
import { num, signClass } from "@/lib/format";
import { kindOf, pnlOf, type OrderHistoryEntry } from "@/lib/history";
import { Stat } from "./Ui";

// Ported from the legacy UI lines 720-746 (openDetail / openTrade).
// openDetail is generic (used for both trade rows and action-log / timeline rows);
// openTrade builds the trade-specific content. Both collapse into this one dialog,
// driven by `content` built by the caller (see buildTradeDetail below, or raw JSX
// for the actions-log / timeline case).

export type Fill = {
  order_id?: number | string;
  created_at?: string;
  side?: string;
  size?: number;
  price?: number | string;
  role?: string;
  commission?: number | string;
};

export function buildTradeDetail(o: OrderHistoryEntry, fills: Fill[]): ReactNode {
  const m = o.meta_data || {};
  const closing = pnlOf(o) != null;
  const orderFills = fills.filter((f) => String(f.order_id) === String(o.id));
  const pnl = +m.pnl;
  const entrySide = o.side === "sell" ? "LONG" : "SHORT";

  const cards: ReactNode[] = [
    <Stat key="symbol" label="Symbol" value={o.product_symbol ?? "–"} />,
    <Stat key="type" label="Type" value={kindOf(o)} />,
    <Stat key="side" label="Side" value={(o.side || "").toUpperCase()} cls={o.side === "buy" ? "g" : "r"} />,
    <Stat key="size" label="Size" value={o.size ?? "–"} />,
  ];
  if (closing) {
    cards.push(
      <Stat key="position" label="Position" value={entrySide} cls={entrySide === "LONG" ? "g" : "r"} />,
      <Stat key="entryexit" label="Entry → Exit" value={`${num(m.entry_price)} → ${num(m.avg_exit_price)}`} />,
      <Stat key="pnl" label="P&L" value={(pnl > 0 ? "+" : "") + num(pnl, 4)} cls={signClass(pnl)} />,
      <Stat key="netpnl" label="Net P&L" value={m.net_pnl == null ? "–" : num(m.net_pnl, 4)} cls={signClass(+m.net_pnl)} />,
      <Stat key="roe" label="ROE" value={m.roe == null ? "–" : num(m.roe, 2) + "%"} cls={signClass(+m.roe)} />,
    );
  }
  cards.push(
    <Stat
      key="state"
      label="State"
      value={o.state + (o.cancellation_reason ? ` (${o.cancellation_reason.replace(/_/g, " ")})` : "")}
    />,
    <Stat key="avgfill" label="Avg fill" value={o.average_fill_price ? num(o.average_fill_price) : "–"} />,
    <Stat key="limit" label="Limit price" value={o.limit_price ? num(o.limit_price) : "–"} />,
    <Stat key="trigger" label="Trigger / stop" value={o.stop_price ? num(o.stop_price) : "–"} />,
    <Stat key="fee" label="Fee" value={num(o.paid_commission, 4)} />,
    <Stat key="created" label="Created" value={o.created_at ? new Date(o.created_at).toLocaleString() : "–"} />,
    <Stat key="updated" label="Updated" value={o.updated_at ? new Date(o.updated_at).toLocaleString() : "–"} />,
    <Stat key="orderno" label="Order #" value={String(o.id ?? "–")} />,
  );

  return (
    <>
      <h2 style={{ color: "var(--text)" }}>
        {kindOf(o)} {(o.side || "").toUpperCase()} {o.size} × {o.product_symbol}
      </h2>
      <div className="stats" style={{ marginTop: 6 }}>{cards}</div>
      {(o.bracket_stop_loss_price || o.bracket_take_profit_price) && (
        <p className="muted">
          Bracket: SL {o.bracket_stop_loss_price || "–"} · TP {o.bracket_take_profit_price || "–"}
        </p>
      )}
      <h2>Fills ({orderFills.length})</h2>
      {orderFills.length ? (
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Side</th>
                <th>Size</th>
                <th>Price</th>
                <th>Role</th>
                <th>Fee</th>
              </tr>
            </thead>
            <tbody>
              {orderFills.map((f, i) => (
                <tr key={i}>
                  <td>{f.created_at ? new Date(f.created_at).toLocaleString() : "–"}</td>
                  <td className={f.side === "buy" ? "g" : "r"}>{(f.side || "").toUpperCase()}</td>
                  <td>{f.size ?? "–"}</td>
                  <td>{num(f.price)}</td>
                  <td>{f.role ?? "–"}</td>
                  <td>{num(f.commission, 4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="muted">No fills recorded for this order in the latest 200 fills.</div>
      )}
      <details>
        <summary className="muted" style={{ cursor: "pointer" }}>Raw order JSON</summary>
        <pre>{JSON.stringify(o, null, 2)}</pre>
      </details>
    </>
  );
}

export function DetailDialog({ content, onClose }: { content: ReactNode | null; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    if (content) requestAnimationFrame(() => dialogRef.current?.showModal());
    else dialogRef.current?.close();
  }, [content]);

  function close() {
    dialogRef.current?.close();
    onClose();
  }

  return (
    <dialog ref={dialogRef} style={{ maxWidth: 760, width: "94vw" }} onCancel={(e) => { e.preventDefault(); close(); }}>
      {content && (
        <div style={{ padding: 4 }}>
          <div>{content}</div>
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 12 }}>
            <button type="button" className="btn ghost" onClick={close}>Close</button>
          </div>
        </div>
      )}
    </dialog>
  );
}
