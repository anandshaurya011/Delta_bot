"use client";
// Ported from backend/static/index.html lines 489-545 (orderBody, submitOrder,
// loadTrade, checkSize, the `.pct` buttons, levSet) plus the position-level
// "Close" button (closePos, lines 487-490) for the current symbol.
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { num } from "@/lib/format";
import { useSymbol } from "@/lib/symbol";
import { useToast } from "@/components/Toast";
import { useConfirm } from "@/components/ConfirmDialog";
import { Stat } from "@/components/Ui";
import { buildOrderBody, describeOrder, sizeFromPercent } from "@/lib/trade";

type Leverage = { leverage: number };
type MaxContractsRow = {
  leverage: number;
  margin_per_contract: number;
  max_contracts: number;
  long_liq_price: number;
  short_liq_price: number;
  liq_distance_pct: number;
  is_current_account_leverage: boolean;
  entry_price: number;
  maintenance_margin_rate: number;
};
type Balance = { available_balance: number | string };
type PriceResp = { price: number };

const PCTS = [25, 50, 75, 100];

export default function TradePage() {
  const [symbol] = useSymbol();
  const toast = useToast();
  const confirm = useConfirm();
  const qc = useQueryClient();

  const [size, setSize] = useState("1");
  const [price, setPrice] = useState("");
  const [sl, setSl] = useState("");
  const [tp, setTp] = useState("");
  const [levNew, setLevNew] = useState<string>("");
  const [stamp, setStamp] = useState("");

  const q = encodeURIComponent(symbol);
  const priceQS = price ? `?price=${encodeURIComponent(price)}` : "";

  const levQ = useQuery({
    queryKey: ["leverage", symbol],
    queryFn: () => api<Leverage>("GET", `/leverage/${q}`),
  });
  const maxcQ = useQuery({
    queryKey: ["max-contracts", symbol, price],
    queryFn: () => api<MaxContractsRow[]>("GET", `/max-contracts/${q}${priceQS}`),
  });
  const balQ = useQuery({
    queryKey: ["balances", "USD"],
    queryFn: () => api<Balance[]>("GET", "/balances?asset=USD"),
  });
  const priceQ = useQuery({
    queryKey: ["price", symbol],
    queryFn: () => api<PriceResp>("GET", `/price/${q}`),
  });

  const rows = maxcQ.data ?? [];
  const lev = levQ.data;
  const cur = rows.find((r) => r.is_current_account_leverage) ?? rows[rows.length - 1];
  const maxNow = cur ? cur.max_contracts : 0;
  const avail = balQ.data?.[0] ? +balQ.data[0].available_balance : 0;

  useEffect(() => {
    if (!lev) return;
    setLevNew((prev) => (rows.some((r) => String(r.leverage) === prev) ? prev : String(lev.leverage)));
    setStamp(new Date().toLocaleTimeString());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lev, rows]);

  function invalidateAll() {
    qc.invalidateQueries({ queryKey: ["leverage", symbol] });
    qc.invalidateQueries({ queryKey: ["max-contracts", symbol] });
    qc.invalidateQueries({ queryKey: ["balances"] });
    qc.invalidateQueries({ queryKey: ["price", symbol] });
    qc.invalidateQueries({ queryKey: ["positions"] });
  }

  const order = useMutation({
    mutationFn: ({ side, body }: { side: "buy" | "sell"; body: ReturnType<typeof buildOrderBody> }) =>
      api<{ id?: unknown; order_id?: unknown }>("POST", `/orders/${side}`, body),
    onSuccess: (r) => {
      toast(`Order placed (id ${r.id ?? r.order_id ?? "ok"})`);
      invalidateAll();
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Failed to place order", "err"),
  });
  const setLeverage = useMutation({
    mutationFn: (v: number) => api("PUT", `/leverage/${q}`, { leverage: v }),
    onSuccess: () => {
      toast("Leverage updated");
      invalidateAll();
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Failed to update leverage", "err"),
  });
  const closePos = useMutation({
    mutationFn: () => api<{ detail?: string }>("POST", `/positions/${q}/close`),
    onSuccess: (r) => {
      toast(r?.detail || "Close order placed");
      invalidateAll();
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Failed to close position", "err"),
  });

  async function submitOrder(side: "buy" | "sell") {
    const body = buildOrderBody({ symbol, size, price, sl, tp });
    if (!(body.size > 0)) {
      toast("Size must be a positive whole number", "err");
      return;
    }
    const desc = describeOrder(side, body);
    if (!(await confirm(desc))) return;
    order.mutate({ side, body });
  }

  async function onClosePos() {
    if (!(await confirm(`Close the ${symbol} position at market?`))) return;
    closePos.mutate();
  }

  async function onSetLeverage() {
    const v = +levNew;
    if (!(v > 0)) {
      toast("Select a leverage", "err");
      return;
    }
    if (lev && v === lev.leverage) {
      toast(`Already at ${v}x`);
      return;
    }
    if (!(await confirm(`Set ${symbol} leverage to ${v}x?`))) return;
    setLeverage.mutate(v);
  }

  function onPct(pct: number) {
    if (!(maxNow > 0)) {
      toast("No contracts affordable right now", "err");
      return;
    }
    const n = sizeFromPercent(maxNow, pct);
    setSize(String(n));
    toast(`${pct}% of max (${maxNow}) = ${n} contract${n > 1 ? "s" : ""}`);
  }

  const sizeNum = +size;
  const sizeWarn = sizeNum > maxNow ? `Size exceeds the ${maxNow} contract(s) you can afford at current leverage.` : "";

  return (
    <div>
      <section>
        <h2>Place order</h2>
        <div className="stats" style={{ margin: 0 }}>
          <Stat label="Available (USD)" value={num(avail)} />
          <Stat label="Leverage" value={lev ? `${lev.leverage}x` : "–"} />
          <Stat label="Price" value={num(priceQ.data?.price)} />
          <Stat label="Margin / contract" value={cur ? num(cur.margin_per_contract, 4) : "–"} />
          <Stat label="Long liq (est.)" value={cur ? num(cur.long_liq_price) : "–"} cls="g" />
          <Stat label="Short liq (est.)" value={cur ? num(cur.short_liq_price) : "–"} cls="r" />
          <Stat label="Max contracts now" value={String(maxNow)} cls={maxNow > 0 ? "g" : "r"} />
        </div>
        <div className="muted">
          {stamp ? `updated ${stamp}` : ""}{" "}
          <button
            type="button"
            className="btn ghost sm"
            onClick={() => {
              levQ.refetch();
              maxcQ.refetch();
              balQ.refetch();
              priceQ.refetch();
            }}
          >
            Refresh
          </button>
        </div>

        <div className="row" style={{ marginTop: 14 }}>
          <label>
            Size
            <input type="number" min={1} step={1} style={{ width: 90 }} value={size} onChange={(e) => setSize(e.target.value)} />
          </label>
          <label>
            Limit price (blank = market)
            <input type="number" step="any" style={{ width: 130 }} value={price} onChange={(e) => setPrice(e.target.value)} />
          </label>
          <label>
            Stop-loss
            <input type="number" step="any" style={{ width: 110 }} value={sl} onChange={(e) => setSl(e.target.value)} />
          </label>
          <label>
            Take-profit
            <input type="number" step="any" style={{ width: 110 }} value={tp} onChange={(e) => setTp(e.target.value)} />
          </label>
          <div className="row" style={{ gap: 6 }}>
            {PCTS.map((pct) => (
              <button key={pct} type="button" className="btn ghost sm" onClick={() => onPct(pct)}>
                {pct}%
              </button>
            ))}
          </div>
          <button type="button" className="btn buy" disabled={order.isPending} onClick={() => submitOrder("buy")}>
            Buy
          </button>
          <button type="button" className="btn sell" disabled={order.isPending} onClick={() => submitOrder("sell")}>
            Sell
          </button>
          <button type="button" className="btn ghost" disabled={closePos.isPending} onClick={onClosePos}>
            Close position
          </button>
        </div>
        {sizeWarn && <div className="r" style={{ marginTop: 8 }}>{sizeWarn}</div>}
      </section>

      <section>
        <h2>Leverage</h2>
        <div className="row">
          <label>
            New leverage
            <select value={levNew} onChange={(e) => setLevNew(e.target.value)}>
              {rows.map((r) => (
                <option key={r.leverage} value={r.leverage}>
                  {r.leverage}x{lev && r.leverage === lev.leverage ? " (current)" : ""}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="btn" disabled={setLeverage.isPending} onClick={onSetLeverage}>
            Set
          </button>
        </div>
      </section>

      <section>
        <h2>Max contracts by leverage</h2>
        <div className="tablewrap">
          {rows.length === 0 ? (
            <div className="muted">Nothing to show.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Leverage</th>
                  <th>Margin / contract (USD)</th>
                  <th>Max contracts</th>
                  <th>Long liq. price</th>
                  <th>Short liq. price</th>
                  <th>Distance to liq.</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.leverage}>
                    <td className={r.is_current_account_leverage ? "w" : undefined}>
                      {r.leverage}x{r.is_current_account_leverage ? " ★" : ""}
                    </td>
                    <td>{num(r.margin_per_contract, 4)}</td>
                    <td>{r.max_contracts}</td>
                    <td className="g">{num(r.long_liq_price)}</td>
                    <td className="r">{num(r.short_liq_price)}</td>
                    <td>{num(r.liq_distance_pct, 2)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="muted" style={{ marginTop: 8 }}>
          {rows.length
            ? `Liquidation prices are estimates for an entry at ${num(rows[0]?.entry_price)} (isolated margin, maintenance margin ${num(
                (rows[0]?.maintenance_margin_rate ?? 0) * 100,
                2,
              )}%). Fees, funding and size-based margin tiers are ignored.${price ? " Using your limit price as the entry." : ""}`
            : ""}
        </div>
      </section>
    </div>
  );
}
