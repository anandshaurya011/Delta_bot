// Ported from the legacy UI lines ~433-460 (kindOf, pnlOf, when) and
// ~749-797 (closedTrades / trSort comparator logic).

export type OrderHistoryEntry = {
  id?: number | string;
  product_symbol?: string;
  side?: string;
  order_type?: string;
  stop_order_type?: string;
  trail_amount?: number | string | null;
  state?: string;
  size?: number;
  average_fill_price?: number | string | null;
  limit_price?: number | string | null;
  stop_price?: number | string | null;
  paid_commission?: number | string | null;
  cancellation_reason?: string | null;
  created_at?: string;
  updated_at?: string;
  bracket_stop_loss_price?: number | string | null;
  bracket_take_profit_price?: number | string | null;
  meta_data?: Record<string, any> | null;
  [k: string]: any;
};

// index.html:433-434
//   const kindOf = o => o.stop_order_type === 'stop_loss_order' ? (o.trail_amount ? 'Trailing SL' : 'Stop-loss') : o.stop_order_type === 'take_profit_order' ? 'Take-profit'
//     : o.order_type === 'limit_order' ? 'Limit' : 'Market';
export function kindOf(o: OrderHistoryEntry): string {
  if (o.stop_order_type === "stop_loss_order") return o.trail_amount ? "Trailing SL" : "Stop-loss";
  if (o.stop_order_type === "take_profit_order") return "Take-profit";
  if (o.order_type === "limit_order") return "Limit";
  return "Market";
}

// index.html:435
//   const pnlOf = o => { const m = o.meta_data || {}; return m.entry_price ? m : null; };  // only closing fills carry entry/exit
// Adapted to return the parsed P&L number (or null) per the Task 10 brief's stated contract.
export function pnlOf(o: OrderHistoryEntry): number | null {
  const m = o.meta_data || {};
  if (!m.entry_price) return null;
  if (m.pnl == null || m.pnl === "") return null;
  const v = +m.pnl;
  return isNaN(v) ? null : v;
}

export type SortKey = "time" | "pnl";

export type TradeRow = {
  time: number;
  pnl: number;
  [k: string]: any;
};

// index.html:790 — sorted = list.slice().sort((a, b) => { const x = a[trSort.key], y = b[trSort.key]; return (x < y ? -1 : x > y ? 1 : 0) * trSort.dir; });
// trSort.dir is -1 for "time desc" (newest first, the default) and 1 for ascending.
export function sortTrades<T extends TradeRow>(rows: T[], key: SortKey, dir: 1 | -1): T[] {
  return rows
    .map((row, i) => ({ row, i }))
    .sort((a, b) => {
      const x = a.row[key];
      const y = b.row[key];
      const cmp = (x < y ? -1 : x > y ? 1 : 0) * dir;
      return cmp !== 0 ? cmp : a.i - b.i;
    })
    .map((x) => x.row);
}
