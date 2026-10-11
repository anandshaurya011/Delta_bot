import { describe, expect, it } from "vitest";
import { kindOf, pnlOf, sortTrades } from "./history";

describe("pnlOf", () => {
  it("returns the parsed number when meta_data carries a closing pnl", () => {
    expect(pnlOf({ meta_data: { entry_price: 100, pnl: "12.5" } })).toBe(12.5);
  });
  it("returns null when there is no entry_price (order never closed anything)", () => {
    expect(pnlOf({ meta_data: {} })).toBeNull();
    expect(pnlOf({})).toBeNull();
  });
});

describe("kindOf", () => {
  it("labels a plain market order", () => {
    expect(kindOf({ order_type: "market_order" })).toBe("Market");
  });
  it("labels a limit order", () => {
    expect(kindOf({ order_type: "limit_order" })).toBe("Limit");
  });
  it("labels a stop-loss order", () => {
    expect(kindOf({ stop_order_type: "stop_loss_order" })).toBe("Stop-loss");
  });
  it("labels a trailing stop-loss order", () => {
    expect(kindOf({ stop_order_type: "stop_loss_order", trail_amount: 5 })).toBe("Trailing SL");
  });
  it("labels a take-profit order", () => {
    expect(kindOf({ stop_order_type: "take_profit_order" })).toBe("Take-profit");
  });
  it("bracket child orders are classified by their own stop_order_type, same as the old UI", () => {
    expect(kindOf({ stop_order_type: "take_profit_order", bracket_take_profit_price: 123 })).toBe("Take-profit");
  });
});

describe("sortTrades", () => {
  const rows = [
    { time: 1, pnl: 5 },
    { time: 3, pnl: -2 },
    { time: 2, pnl: -2 },
  ];
  it("sorts by time descending with dir -1", () => {
    expect(sortTrades(rows, "time", -1).map((r) => r.time)).toEqual([3, 2, 1]);
  });
  it("sorts by pnl ascending with dir 1 and is stable for ties", () => {
    const sorted = sortTrades(rows, "pnl", 1);
    expect(sorted.map((r) => r.pnl)).toEqual([-2, -2, 5]);
    // tie between time:3 and time:2 (both pnl -2): original order preserved
    expect(sorted.slice(0, 2).map((r) => r.time)).toEqual([3, 2]);
  });
});
