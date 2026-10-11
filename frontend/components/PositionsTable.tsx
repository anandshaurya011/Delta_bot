"use client";
// Ported from the legacy UI lines 387-391 (posTable) and 487-490
// (closePos), shared by the Dashboard page and (in future) elsewhere.
import { num, signClass } from "@/lib/format";
import { api, ApiError } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { useConfirm } from "@/components/ConfirmDialog";

export type Position = {
  symbol: string;
  side: "long" | "short";
  size: number;
  entry_price: number;
  mark_price: number;
  unrealized_pnl: number;
  realized_pnl: number;
};

export function PositionsTable({ positions, onClosed }: { positions: Position[]; onClosed: () => void }) {
  const toast = useToast();
  const confirm = useConfirm();

  async function closePos(symbol: string) {
    if (!(await confirm(`Close the ${symbol} position at market?`))) return;
    try {
      const r = await api<{ detail?: string }>("POST", `/positions/${encodeURIComponent(symbol)}/close`);
      toast(r?.detail || "Close order placed");
      onClosed();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to close position", "err");
    }
  }

  if (positions.length === 0) {
    return <div className="muted">No open positions.</div>;
  }

  return (
    <div className="tablewrap">
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
          {positions.map((p) => (
            <tr key={p.symbol}>
              <td>{p.symbol}</td>
              <td className={p.side === "long" ? "g" : "r"}>{p.side.toUpperCase()}</td>
              <td>{num(Math.abs(p.size), 0)}</td>
              <td>{num(p.entry_price)}</td>
              <td>{num(p.mark_price)}</td>
              <td className={signClass(p.unrealized_pnl)}>{(p.unrealized_pnl > 0 ? "+" : "") + num(p.unrealized_pnl, 4)}</td>
              <td>{num(p.realized_pnl, 4)}</td>
              <td>
                <button type="button" className="btn ghost sm" onClick={() => closePos(p.symbol)}>
                  Close
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
