import { expect, it } from "vitest";
import { buildOrderBody, describeOrder, sizeFromPercent } from "./trade";

it("buildOrderBody truncates size and nulls blanks", () => {
  expect(buildOrderBody({ symbol: "btcusd", size: "3.9", price: "", sl: "", tp: "" })).toEqual({ symbol: "BTCUSD", size: 3, price: null, stop_loss: null, take_profit: null });
  expect(buildOrderBody({ symbol: "BTCUSD", size: "1", price: "80000", sl: "79000", tp: "82000" })).toMatchObject({ price: 80000, stop_loss: 79000, take_profit: 82000 });
});
it("describeOrder warns it is a real order", () => {
  const t = describeOrder("buy", { symbol: "BTCUSD", size: 2, price: null, stop_loss: 79000, take_profit: null });
  expect(t).toContain("BUY 2 × BTCUSD"); expect(t).toContain("at market"); expect(t).toContain("stop-loss 79000"); expect(t).toContain("REAL order");
});
it("sizeFromPercent is at least 1 whole contract", () => {
  expect(sizeFromPercent(14, 25)).toBe(3); expect(sizeFromPercent(2, 10)).toBe(1);
});
