// Ported from the legacy UI lines 493-504 (orderBody, submitOrder's
// confirmation text) and 537 (the `.pct` button handler's size-from-percent math).

export type OrderForm = { symbol: string; size: string; price: string; sl: string; tp: string };

export type OrderBody = {
  symbol: string;
  size: number;
  price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
};

export function buildOrderBody(f: OrderForm): OrderBody {
  const n = (s: string): number | null => (s === "" ? null : +s);
  return {
    symbol: f.symbol.toUpperCase(),
    size: Math.trunc(+f.size),
    price: n(f.price),
    stop_loss: n(f.sl),
    take_profit: n(f.tp),
  };
}

export function describeOrder(side: "buy" | "sell", body: OrderBody): string {
  return (
    `${side.toUpperCase()} ${body.size} × ${body.symbol}\n` +
    (body.price ? `limit @ ${body.price}` : "at market") +
    (body.stop_loss ? `\nstop-loss ${body.stop_loss}` : "") +
    (body.take_profit ? `\ntake-profit ${body.take_profit}` : "") +
    "\n\nThis is a REAL order."
  );
}

export function sizeFromPercent(maxContracts: number, pct: number): number {
  return Math.max(1, Math.floor((maxContracts * pct) / 100));
}
