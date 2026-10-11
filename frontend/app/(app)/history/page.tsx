"use client";
import { Fragment, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { num, signClass } from "@/lib/format";
import { useToast } from "@/components/Toast";
import { useConfirm } from "@/components/ConfirmDialog";
import { Stat, Tag, Chip } from "@/components/Ui";
import { kindOf, pnlOf, sortTrades, type OrderHistoryEntry } from "@/lib/history";
import { buildTradeDetail, DetailDialog, type Fill } from "@/components/TradeDetail";

// Ported from the legacy UI lines 213-264 (markup for the three history
// views), 703-918 (loadHistoryData, renderTrades/drawChart, renderHistory/describe/
// outcome/repeatSpec, renderTimeline).

type ActionEvent = {
  ts: number;
  action: string;
  ok: boolean;
  request?: { body?: any; [k: string]: any };
  result?: any;
};

const ago = (ts: number) => {
  const d = Math.floor(Date.now() / 1000 - ts);
  return d < 60 ? d + "s ago" : d < 3600 ? Math.floor(d / 60) + "m ago" : d < 86400 ? Math.floor(d / 3600) + "h ago" : Math.floor(d / 86400) + "d ago";
};
const tsOf = (o: OrderHistoryEntry) => new Date(o.updated_at || o.created_at || 0).getTime() / 1000;
const flat = (e: ActionEvent) => ({ ...e.request, ...(e.request?.body ?? {}) });
const up = (x: any) => String(x || "").toUpperCase();

function describe(e: ActionEvent): string {
  const r: any = flat(e);
  switch (e.action) {
    case "buy":
    case "sell":
      return (
        `${up(e.action)} ${r.size} × ${r.symbol} ` +
        (r.price ? `limit @ ${r.price}` : "at market") +
        (r.stop_loss ? ` · SL ${r.stop_loss}` : "") +
        (r.take_profit ? ` · TP ${r.take_profit}` : "")
      );
    case "stop-loss":
    case "take-profit":
      return `${up(e.action)} ${r.symbol}: ${r.side} ${r.size} @ ${r.stop_price}`;
    case "trailing-stop":
      return `Trailing stop ${r.symbol}: ${r.side} ${r.size}, trail ${r.trail_amount}`;
    case "close":
      return `Close ${r.symbol} position`;
    case "cancel":
      return `Cancel order #${r.order_id} (${r.symbol})`;
    case "cancel-all":
      return r.symbol ? `Cancel all orders for ${r.symbol}` : "Cancel all orders";
    case "cancel-trailing-stop":
      return `Cancel trailing stop on ${r.symbol}`;
    case "set-leverage":
      return `Set ${r.symbol} leverage to ${r.leverage}x`;
    case "edit-bracket":
      return (
        `Edit bracket ${r.symbol}` +
        (r.stop_loss ? ` · SL ${r.stop_loss}` : "") +
        (r.take_profit ? ` · TP ${r.take_profit}` : "") +
        (r.trail_amount ? ` · trail ${r.trail_amount}` : "")
      );
    default:
      return e.action;
  }
}
function outcome(e: ActionEvent): string {
  if (!e.ok) return String(e.result).replace(/^\{'error': \{'code': '([^']+)'.*$/, "error: $1").slice(0, 140);
  const x = e.result;
  if (x && typeof x === "object" && !Array.isArray(x)) {
    if (x.id) return `order #${x.id}` + (x.state ? ` · ${x.state}` : "") + (x.average_fill_price ? ` @ ${num(x.average_fill_price)}` : "");
    if (x.leverage) return `leverage ${x.leverage}x`;
    if (x.detail) return x.detail;
  }
  if (Array.isArray(x)) return `${x.length} item(s)`;
  return "ok";
}
function repeatSpec(e: ActionEvent): [string, string, any?] | null {
  const r: any = e.request || {};
  const b = r.body;
  switch (e.action) {
    case "buy":
    case "sell":
    case "stop-loss":
    case "take-profit":
    case "trailing-stop":
      return ["POST", `/orders/${e.action}`, b];
    case "set-leverage":
      return ["PUT", `/leverage/${encodeURIComponent(r.symbol)}`, b];
    case "edit-bracket":
      return ["PUT", "/orders/bracket", b];
    case "close":
      return ["POST", `/positions/${encodeURIComponent(r.symbol)}/close`];
    case "cancel-all":
      return ["DELETE", "/orders" + (r.symbol ? `?symbol=${encodeURIComponent(r.symbol)}` : "")];
    case "cancel-trailing-stop":
      return ["DELETE", `/orders/${encodeURIComponent(r.symbol)}/trailing-stop`];
    default:
      return null; // cancel by order id: that order no longer exists
  }
}

type View = "trades" | "actions" | "timeline";
const VIEWS: [View, string][] = [
  ["trades", "Trades & P&L"],
  ["actions", "Actions log"],
  ["timeline", "Combined timeline"],
];

export default function HistoryPage() {
  const toast = useToast();
  const confirm = useConfirm();
  const qc = useQueryClient();
  const [view, setView] = useState<View>("trades");
  const [detail, setDetail] = useState<React.ReactNode | null>(null);

  const historyQ = useQuery({
    queryKey: ["history", "actions"],
    queryFn: () => api<ActionEvent[]>("GET", "/history?limit=1000"),
  });
  const ordersHistQ = useQuery({
    queryKey: ["history", "orders"],
    queryFn: () => api<OrderHistoryEntry[]>("GET", "/orders/history?limit=200"),
  });
  const fillsQ = useQuery({
    queryKey: ["history", "fills"],
    queryFn: () => api<Fill[]>("GET", "/fills?limit=200"),
  });

  const hAll = historyQ.data ?? [];
  const hOrders = ordersHistQ.data ?? [];
  const hFills = fillsQ.data ?? [];

  function reloadAll() {
    qc.invalidateQueries({ queryKey: ["history"] });
  }

  function openTrade(o: OrderHistoryEntry) {
    setDetail(buildTradeDetail(o, hFills));
  }
  function openActionDetail(e: ActionEvent) {
    setDetail(
      <>
        <h2 style={{ color: "var(--text)" }}>{describe(e)}</h2>
        <p className={e.ok ? "g" : "r"}>{(e.ok ? "✓ " : "✗ ") + outcome(e)}</p>
        <small className="muted">Request</small>
        <pre>{JSON.stringify(e.request, null, 2)}</pre>
        <small className="muted">Result</small>
        <pre>{typeof e.result === "string" ? e.result : JSON.stringify(e.result, null, 2)}</pre>
        {repeatSpec(e) && (
          <div style={{ marginTop: 10 }}>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setDetail(null);
                repeatAction(e);
              }}
            >
              ↻ Repeat this action
            </button>
          </div>
        )}
      </>,
    );
  }

  async function repeatAction(e: ActionEvent) {
    const spec = repeatSpec(e);
    if (!spec) return toast("This action can’t be repeated.", "err");
    if (!(await confirm(`Repeat this action now?\n\n${describe(e)}\n\nThis is a REAL request against the live account.`))) return;
    try {
      const r = await api<any>(spec[0], spec[1], spec[2]);
      toast("Done: " + (r && r.id ? "order #" + r.id : "ok"));
      reloadAll();
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "Failed", "err");
    }
  }

  return (
    <div>
      <div className="chips" style={{ marginBottom: 14 }}>
        {VIEWS.map(([k, label]) => (
          <Chip key={k} on={view === k} onClick={() => setView(k)}>
            {label}
          </Chip>
        ))}
      </div>

      {view === "trades" && (
        <TradesView hOrders={hOrders} loading={ordersHistQ.isFetching} onRefresh={reloadAll} onOpenTrade={openTrade} />
      )}
      {view === "actions" && (
        <ActionsView hAll={hAll} loading={historyQ.isFetching} onRefresh={reloadAll} onOpenAction={openActionDetail} onRepeat={repeatAction} />
      )}
      {view === "timeline" && (
        <TimelineView
          hAll={hAll}
          hOrders={hOrders}
          loading={historyQ.isFetching || ordersHistQ.isFetching}
          onRefresh={reloadAll}
          onOpenTrade={openTrade}
          onOpenAction={openActionDetail}
        />
      )}

      <DetailDialog content={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

// ---- Trades & P&L ---------------------------------------------------------

type ClosedTrade = {
  o: OrderHistoryEntry;
  time: number;
  symbol: string;
  size: number | string;
  entry: number;
  exit: number;
  gross: number;
  net: number;
  roe: number | null;
  by: string;
  dir: "LONG" | "SHORT";
};

function closedTrades(hOrders: OrderHistoryEntry[]): ClosedTrade[] {
  return hOrders
    .filter((o) => pnlOf(o) != null)
    .map((o) => {
      const m = o.meta_data || {};
      const gross = +m.pnl;
      const net = m.net_pnl == null ? gross : +m.net_pnl;
      return {
        o,
        time: tsOf(o),
        symbol: o.product_symbol ?? "",
        size: o.size ?? "–",
        entry: +m.entry_price,
        exit: +m.avg_exit_price,
        gross,
        net,
        roe: m.roe == null ? null : +m.roe,
        by: kindOf(o),
        dir: o.side === "sell" ? "LONG" : "SHORT",
      };
    });
}

function PnlChart({ trades, onOpenTrade }: { trades: ClosedTrade[]; onOpenTrade: (o: OrderHistoryEntry) => void }) {
  const n = trades.length;
  if (!n) return <div className="muted">No closed trades match these filters.</div>;
  const W = 800, H = 260, L = 56, R = 12, T = 12, B = 26;
  const pw = W - L - R, ph = H - T - B;
  let run = 0;
  const cum = trades.map((t) => (run += t.net));
  const all = [0, ...trades.map((t) => t.net), ...cum];
  let lo = Math.min(...all), hi = Math.max(...all);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08;
  lo -= pad; hi += pad;
  const x = (i: number) => L + (n === 1 ? pw / 2 : (i * pw) / (n - 1));
  const y = (v: number) => T + ph - ((v - lo) / (hi - lo)) * ph;
  const bw = Math.max(3, Math.min(26, (pw / n) * 0.55));
  const tip = (t: ClosedTrade) =>
    `${new Date(t.time * 1000).toLocaleString()}\n${t.dir} ${t.size} ${t.symbol}  ${num(t.entry)} → ${num(t.exit)}\nnet ${t.net > 0 ? "+" : ""}${num(t.net, 4)}`;

  return (
    <div>
      <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
        Bars = net P&amp;L per trade · line = cumulative net P&amp;L
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", maxHeight: 300, display: "block" }}>
        {[lo + pad, 0, hi - pad].map((v, i) => (
          <g key={i}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} style={{ stroke: "var(--line)", ...(v === 0 ? { strokeWidth: 1.5 } : { strokeDasharray: "3 4" }) }} />
            <text x={L - 6} y={y(v) + 4} textAnchor="end" style={{ fill: "var(--dim)", fontSize: 11 }}>
              {num(v, 2)}
            </text>
          </g>
        ))}
        {trades.map((t, i) => {
          const y0 = y(0), y1 = y(t.net);
          return (
            <rect
              key={"b" + i}
              x={x(i) - bw / 2}
              width={bw}
              y={Math.min(y0, y1)}
              height={Math.max(1.5, Math.abs(y1 - y0))}
              rx={2}
              style={{ fill: `var(--${t.net >= 0 ? "green" : "red"})`, opacity: 0.5, cursor: "pointer" }}
              onClick={() => onOpenTrade(t.o)}
              onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
              onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.5")}
            >
              <title>{tip(t)}</title>
            </rect>
          );
        })}
        <polyline points={cum.map((v, i) => `${x(i)},${y(v)}`).join(" ")} style={{ fill: "none", stroke: "var(--accent)", strokeWidth: 2 }} />
        {cum.map((v, i) => (
          <circle
            key={"c" + i}
            cx={x(i)}
            cy={y(v)}
            r={4}
            style={{ fill: "var(--accent)", cursor: "pointer", stroke: "var(--panel)", strokeWidth: 1.5 }}
            onClick={() => onOpenTrade(trades[i].o)}
          >
            <title>{tip(trades[i]) + `\ncumulative ${v > 0 ? "+" : ""}${num(v, 4)}`}</title>
          </circle>
        ))}
        <text x={L} y={H - 6} style={{ fill: "var(--dim)", fontSize: 11 }}>
          {new Date(trades[0].time * 1000).toLocaleDateString()}
        </text>
        <text x={W - R} y={H - 6} textAnchor="end" style={{ fill: "var(--dim)", fontSize: 11 }}>
          {new Date(trades[n - 1].time * 1000).toLocaleDateString()}
        </text>
      </svg>
    </div>
  );
}

const TR_COLS: [string, string][] = [
  ["time", "Closed"],
  ["symbol", "Symbol"],
  ["dir", "Direction"],
  ["size", "Size"],
  ["entry", "Entry"],
  ["exit", "Exit"],
  ["by", "Closed by"],
  ["gross", "P&L"],
  ["net", "Net P&L"],
  ["roe", "ROE %"],
];
const CLOSED_BY_OPTIONS = ["all", "Market", "Limit", "Stop-loss", "Take-profit", "Trailing SL"];

function TradesView({
  hOrders,
  loading,
  onRefresh,
  onOpenTrade,
}: {
  hOrders: OrderHistoryEntry[];
  loading: boolean;
  onRefresh: () => void;
  onOpenTrade: (o: OrderHistoryEntry) => void;
}) {
  const all = useMemo(() => closedTrades(hOrders), [hOrders]);
  const syms = useMemo(() => [...new Set(all.map((t) => t.symbol))].sort(), [all]);
  const [symSel, setSymSel] = useState("all");
  const [range, setRange] = useState("0");
  const [result, setResult] = useState("all");
  const [by, setBy] = useState("all");
  const [sort, setSort] = useState<{ key: "time" | "symbol" | "dir" | "size" | "entry" | "exit" | "by" | "gross" | "net" | "roe"; dir: 1 | -1 }>({
    key: "time",
    dir: -1,
  });

  const list = useMemo(() => {
    const rg = +range, now = Date.now() / 1000;
    return all
      .filter(
        (t) =>
          (symSel === "all" || t.symbol === symSel) &&
          (!rg || now - t.time <= rg) &&
          (result === "all" || (result === "win") === t.net > 0) &&
          (by === "all" || t.by === by),
      )
      .sort((a, b) => a.time - b.time);
  }, [all, symSel, range, result, by]);

  const wins = list.filter((t) => t.net > 0);
  const losses = list.filter((t) => t.net <= 0);
  const sum = (f: (t: ClosedTrade) => number) => list.reduce((a, t) => a + f(t), 0);
  const net = sum((t) => t.net);
  const gross = sum((t) => t.gross);
  const gw = wins.reduce((a, t) => a + t.net, 0);
  const gl = Math.abs(losses.reduce((a, t) => a + t.net, 0));

  const sorted = useMemo(() => sortTrades(list as any, sort.key as any, sort.dir) as unknown as ClosedTrade[], [list, sort]);

  function toggleSort(k: typeof sort.key) {
    setSort((s) => ({ key: k, dir: s.key === k ? ((-s.dir) as 1 | -1) : -1 }));
  }

  return (
    <section>
      <h2>
        Closed trades &amp; P&amp;L <span className="muted">· {list.length} of {all.length} trades</span>
      </h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <label>
          Symbol
          <select value={symSel} onChange={(e) => setSymSel(e.target.value)}>
            <option value="all">All symbols</option>
            {syms.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          Range
          <select value={range} onChange={(e) => setRange(e.target.value)}>
            <option value="0">All time</option>
            <option value="86400">Last 24 h</option>
            <option value="604800">Last 7 days</option>
            <option value="2592000">Last 30 days</option>
          </select>
        </label>
        <label>
          Result
          <select value={result} onChange={(e) => setResult(e.target.value)}>
            <option value="all">All</option>
            <option value="win">Wins</option>
            <option value="loss">Losses</option>
          </select>
        </label>
        <label>
          Closed by
          <select value={by} onChange={(e) => setBy(e.target.value)}>
            {CLOSED_BY_OPTIONS.map((o) => (
              <option key={o} value={o}>
                {o === "all" ? "Any" : o}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn ghost sm" disabled={loading} onClick={onRefresh}>
          ⟳ Refresh
        </button>
      </div>
      <div className="stats" style={{ marginTop: 0 }}>
        <Stat label="Trades" value={list.length} />
        <Stat
          label="Win rate"
          value={list.length ? num((wins.length / list.length) * 100, 1) + "%" : "–"}
          cls={wins.length >= losses.length ? "g" : "r"}
        />
        <Stat label="Net P&L" value={(net > 0 ? "+" : "") + num(net, 4)} cls={signClass(net)} />
        <Stat label="Gross P&L" value={(gross > 0 ? "+" : "") + num(gross, 4)} cls={signClass(gross)} />
        <Stat label="Fees paid" value={num(gross - net, 4)} />
        <Stat label="Avg win" value={wins.length ? "+" + num(gw / wins.length, 4) : "–"} cls="g" />
        <Stat label="Avg loss" value={losses.length ? num(-gl / losses.length, 4) : "–"} cls="r" />
        <Stat label="Best" value={list.length ? "+" + num(Math.max(...list.map((t) => t.net)), 4) : "–"} cls="g" />
        <Stat label="Worst" value={list.length ? num(Math.min(...list.map((t) => t.net)), 4) : "–"} cls="r" />
        <Stat label="Profit factor" value={gl ? num(gw / gl, 2) : gw ? "∞" : "–"} />
      </div>
      <div style={{ margin: "6px 0 14px" }}>
        <PnlChart trades={list} onOpenTrade={onOpenTrade} />
      </div>
      <div className="tablewrap">
        {sorted.length === 0 ? (
          <div className="muted">No closed trades match these filters.</div>
        ) : (
          <table>
            <thead>
              <tr>
                {TR_COLS.map(([k, label]) => (
                  <th key={k} style={{ cursor: "pointer", userSelect: "none" }} onClick={() => toggleSort(k as typeof sort.key)}>
                    {label}
                    {sort.key === k ? (sort.dir > 0 ? " ▲" : " ▼") : ""}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((x, i) => (
                <tr key={i} style={{ cursor: "pointer" }} onClick={() => onOpenTrade(x.o)}>
                  <td>{new Date(x.time * 1000).toLocaleString()}</td>
                  <td>{x.symbol}</td>
                  <td className={x.dir === "LONG" ? "g" : "r"}>{x.dir}</td>
                  <td>{x.size}</td>
                  <td>{num(x.entry)}</td>
                  <td>{num(x.exit)}</td>
                  <td>{x.by}</td>
                  <td className={signClass(x.gross)}>{(x.gross > 0 ? "+" : "") + num(x.gross, 4)}</td>
                  <td className={signClass(x.net)}>{(x.net > 0 ? "+" : "") + num(x.net, 4)}</td>
                  <td className={x.roe == null ? "d" : signClass(x.roe)}>{x.roe == null ? "–" : num(x.roe, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <p className="muted" style={{ marginBottom: 0 }}>
        Click a bar, a point on the line, or a row to open that trade. Click a column header to sort.
      </p>
    </section>
  );
}

// ---- Actions log ------------------------------------------------------------

function ActionsView({
  hAll,
  loading,
  onRefresh,
  onOpenAction,
  onRepeat,
}: {
  hAll: ActionEvent[];
  loading: boolean;
  onRefresh: () => void;
  onOpenAction: (e: ActionEvent) => void;
  onRepeat: (e: ActionEvent) => void;
}) {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [range, setRange] = useState("86400");
  const [sort, setSort] = useState("new");
  const [action, setAction] = useState("all");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [auto, setAuto] = useState(false);

  useEffect(() => {
    if (!auto) return;
    const id = setInterval(onRefresh, 10000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto]);

  const list = useMemo(() => {
    const q = search.trim().toLowerCase();
    const rg = +range, now = Date.now() / 1000;
    let out = hAll.filter(
      (e) =>
        (action === "all" || e.action === action) &&
        (status === "all" || (status === "ok") === e.ok) &&
        (!rg || now - e.ts <= rg) &&
        (!q || (describe(e) + " " + outcome(e) + " " + JSON.stringify(e.request)).toLowerCase().includes(q)),
    );
    if (sort === "old") out = out.slice().reverse();
    return out;
  }, [hAll, search, status, range, action, sort]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    hAll.forEach((e) => (c[e.action] = (c[e.action] || 0) + 1));
    return c;
  }, [hAll]);

  const okN = list.filter((e) => e.ok).length;

  function toggle(key: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }
  function keyOf(e: ActionEvent) {
    return e.ts + e.action + JSON.stringify(e.request);
  }

  function download(name: string, text: string, type: string) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
  }
  function exportJson() {
    download("delta-history.json", JSON.stringify(list, null, 2), "application/json");
  }
  function exportCsv() {
    const q = (v: unknown) => '"' + String(v ?? "").replace(/"/g, '""') + '"';
    download(
      "delta-history.csv",
      ["time,action,ok,details,outcome", ...list.map((e) => [new Date(e.ts * 1000).toISOString(), e.action, e.ok, describe(e), outcome(e)].map(q).join(","))].join("\n"),
      "text/csv",
    );
  }

  return (
    <section>
      <h2>
        Action history <span className="muted">· showing {list.length} of {hAll.length}</span>
      </h2>
      <div className="stats" style={{ marginTop: 0 }}>
        <Stat label="Shown" value={list.length} />
        <Stat label="Succeeded" value={okN} cls="g" />
        <Stat label="Failed" value={list.length - okN} cls={list.length - okN ? "r" : ""} />
        <Stat label="Orders placed" value={list.filter((e) => e.ok && ["buy", "sell"].includes(e.action)).length} />
        <Stat label="Last action" value={list.length ? ago(Math.max(...list.map((e) => e.ts))) : "–"} />
      </div>
      <div className="chips" style={{ marginBottom: 10 }}>
        {["all", ...Object.keys(counts).sort()].map((a) => (
          <Chip key={a} on={action === a} onClick={() => setAction(a)}>
            {a === "all" ? `All (${hAll.length})` : `${a} (${counts[a]})`}
          </Chip>
        ))}
      </div>
      <div className="row" style={{ marginBottom: 12 }}>
        <label>
          Search
          <input placeholder="symbol, price, error…" style={{ width: 220 }} value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
        <label>
          Status
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="all">All</option>
            <option value="ok">Succeeded</option>
            <option value="fail">Failed</option>
          </select>
        </label>
        <label>
          Range
          <select value={range} onChange={(e) => setRange(e.target.value)}>
            <option value="0">All time</option>
            <option value="3600">Last hour</option>
            <option value="86400">Last 24 h</option>
            <option value="604800">Last 7 days</option>
          </select>
        </label>
        <label>
          Sort
          <select value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="new">Newest first</option>
            <option value="old">Oldest first</option>
          </select>
        </label>
        <button type="button" className="btn ghost sm" disabled={loading} onClick={onRefresh}>
          ⟳ Refresh
        </button>
        <button type="button" className="btn ghost sm" onClick={exportCsv}>
          Export CSV
        </button>
        <button type="button" className="btn ghost sm" onClick={exportJson}>
          Export JSON
        </button>
        <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> live (10s)
        </label>
      </div>
      {list.length === 0 ? (
        <div className="muted">No matching actions.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th></th>
              <th>Time</th>
              <th>Action</th>
              <th>Details</th>
              <th>Outcome</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {list.map((e) => {
              const key = keyOf(e);
              const isOpen = open.has(key);
              return (
                <Fragment key={key}>
                  <tr style={{ cursor: "pointer" }} onClick={() => toggle(key)}>
                    <td style={{ width: 24, textAlign: "center" }}>{isOpen ? "▾" : "▸"}</td>
                    <td title={new Date(e.ts * 1000).toLocaleString()} style={{ textAlign: "left" }}>
                      {new Date(e.ts * 1000).toLocaleTimeString() + " · " + ago(e.ts)}
                    </td>
                    <td style={{ textAlign: "left" }}>
                      <Tag text={e.action} cls={e.ok ? "g" : "r"} />
                    </td>
                    <td style={{ textAlign: "left", whiteSpace: "normal" }}>{describe(e)}</td>
                    <td className={e.ok ? "g" : "r"} style={{ textAlign: "left", whiteSpace: "normal" }}>
                      {(e.ok ? "✓ " : "✗ ") + outcome(e)}
                    </td>
                    <td>
                      {repeatSpec(e) && (
                        <button
                          type="button"
                          className="btn ghost sm"
                          onClick={(ev) => {
                            ev.stopPropagation();
                            onRepeat(e);
                          }}
                        >
                          ↻ Repeat
                        </button>
                      )}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr>
                      <td colSpan={6} style={{ textAlign: "left", whiteSpace: "normal" }}>
                        <div className="row" style={{ alignItems: "start" }}>
                          <div style={{ flex: 1, minWidth: 260 }}>
                            <small className="muted">Request</small>
                            <pre>{JSON.stringify(e.request, null, 2)}</pre>
                          </div>
                          <div style={{ flex: 2, minWidth: 300 }}>
                            <small className="muted">Result</small>
                            <pre>{typeof e.result === "string" ? e.result : JSON.stringify(e.result, null, 2)}</pre>
                          </div>
                        </div>
                        <button
                          type="button"
                          className="btn ghost sm"
                          style={{ marginTop: 8 }}
                          onClick={(ev) => {
                            ev.stopPropagation();
                            navigator.clipboard?.writeText(JSON.stringify(e, null, 2));
                          }}
                        >
                          Copy JSON
                        </button>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---- Combined timeline -------------------------------------------------------

function TimelineView({
  hAll,
  hOrders,
  loading,
  onRefresh,
  onOpenTrade,
  onOpenAction,
}: {
  hAll: ActionEvent[];
  hOrders: OrderHistoryEntry[];
  loading: boolean;
  onRefresh: () => void;
  onOpenTrade: (o: OrderHistoryEntry) => void;
  onOpenAction: (e: ActionEvent) => void;
}) {
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("all");
  const [showCancelled, setShowCancelled] = useState(false);

  const rows = useMemo(() => {
    const out: { ts: number; src: "ex" | "app"; what: string; res: string; cls: string; open: () => void }[] = [];
    if (source !== "app") {
      hOrders
        .filter((o) => showCancelled || o.state !== "cancelled")
        .forEach((o) => {
          const pnl = pnlOf(o);
          const m = o.meta_data || {};
          out.push({
            ts: tsOf(o),
            src: "ex",
            what: `${kindOf(o)} ${(o.side || "").toUpperCase()} ${o.size} × ${o.product_symbol}` + (o.average_fill_price ? ` @ ${num(o.average_fill_price)}` : ""),
            res:
              pnl != null
                ? `closed ${m.entry_price ? num(m.entry_price) + " → " + num(m.avg_exit_price) : ""} · P&L ${(pnl > 0 ? "+" : "") + num(pnl, 4)}`
                : o.state + (o.cancellation_reason ? ` (${o.cancellation_reason.replace(/_/g, " ")})` : ""),
            cls: pnl != null ? signClass(pnl) : o.state === "closed" ? "g" : "d",
            open: () => onOpenTrade(o),
          });
        });
    }
    if (source !== "ex") {
      hAll.forEach((e) => {
        out.push({
          ts: e.ts,
          src: "app",
          what: describe(e),
          res: (e.ok ? "✓ " : "✗ ") + outcome(e),
          cls: e.ok ? "g" : "r",
          open: () => onOpenAction(e),
        });
      });
    }
    const q = search.trim().toLowerCase();
    return out
      .filter((r) => !q || (r.what + " " + r.res).toLowerCase().includes(q))
      .sort((a, b) => b.ts - a.ts)
      .slice(0, 400);
  }, [hAll, hOrders, source, showCancelled, search, onOpenTrade, onOpenAction]);

  return (
    <section>
      <h2>
        Combined timeline <span className="muted">· {rows.length} events</span>
      </h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <label>
          Search
          <input placeholder="symbol, type, price…" style={{ width: 220 }} value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
        <label>
          Source
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="all">Exchange + this app</option>
            <option value="ex">Exchange only</option>
            <option value="app">This app only</option>
          </select>
        </label>
        <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
          <input type="checkbox" checked={showCancelled} onChange={(e) => setShowCancelled(e.target.checked)} /> show cancelled
        </label>
        <button type="button" className="btn ghost sm" disabled={loading} onClick={onRefresh}>
          ⟳ Refresh
        </button>
      </div>
      <div className="tablewrap">
        {rows.length === 0 ? (
          <div className="muted">Nothing to show.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Source</th>
                <th>Event</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} style={{ cursor: "pointer" }} onClick={r.open}>
                  <td style={{ textAlign: "left" }} title={new Date(r.ts * 1000).toLocaleString()}>
                    {new Date(r.ts * 1000).toLocaleString()}
                  </td>
                  <td style={{ textAlign: "left" }}>
                    <Tag text={r.src === "ex" ? "Exchange" : "This app"} cls={r.src === "ex" ? "w" : "d"} />
                  </td>
                  <td style={{ textAlign: "left" }}>{r.what}</td>
                  <td className={r.cls} style={{ textAlign: "left" }}>
                    {r.res}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
