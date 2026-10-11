"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { num } from "@/lib/format";
import { useSymbol } from "@/lib/symbol";
import { useToast } from "@/components/Toast";
import { Stat, Chip } from "@/components/Ui";
import { AnalysisTable } from "@/components/AnalysisTable";
import { TFS, pushSnapshot, toggleSelected, type SnapState } from "@/lib/analysis";

type AnalysisResponse = {
  symbol: string;
  price: number;
  leverage: number;
  max_contracts: number;
  volume: number;
  oi_contracts: number;
  oi_value_usd: number;
  oi_change_usd_6h: number | null;
  timeframes: any[];
};

const EVERY_OPTIONS = [5, 10, 20, 30, 60, 120, 300];

function loadSel(): string[] {
  try {
    const v = JSON.parse(window.localStorage.getItem("aSel") ?? "");
    if (Array.isArray(v)) return v;
  } catch {
    // ignore
  }
  return ["15m", "1h", "4h", "1d"];
}
function loadMark(): string[] {
  try {
    const v = JSON.parse(window.localStorage.getItem("aMark") ?? "");
    if (Array.isArray(v)) return v;
  } catch {
    // ignore
  }
  return [];
}
function loadEvery(): number {
  try {
    const v = Number(window.localStorage.getItem("aEvery"));
    if (EVERY_OPTIONS.includes(v)) return v;
  } catch {
    // ignore
  }
  return 30;
}

export default function AnalysisPage() {
  const [symbol] = useSymbol();
  const toast = useToast();

  const [sel, setSel] = useState<string[]>(["15m", "1h", "4h", "1d"]);
  const [marked, setMarked] = useState<string[]>([]);
  const [period, setPeriod] = useState(14);
  const [threshold, setThreshold] = useState(25);
  const [overbought, setOverbought] = useState(70);
  const [oversold, setOversold] = useState(30);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [every, setEvery] = useState(30);
  const [state, setState] = useState<SnapState>({ snaps: [], idx: -1, key: "" });
  const [loading, setLoading] = useState(false);

  const [ready, setReady] = useState(false);
  const loadingRef = useRef(false);
  const stateRef = useRef(state);
  stateRef.current = state;

  // Load persisted settings before the first fetch fires, so it never races
  // a default-selection request against the real one from localStorage.
  useEffect(() => {
    setSel(loadSel());
    setMarked(loadMark());
    setEvery(loadEvery());
    setReady(true);
  }, []);

  function toggleTf(t: string) {
    setSel((prev) => {
      const next = toggleSelected(prev, t);
      try {
        window.localStorage.setItem("aSel", JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  }
  function toggleAll() {
    setSel((prev) => {
      const next = prev.length === TFS.length ? [] : [...TFS];
      try {
        window.localStorage.setItem("aSel", JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  }
  function toggleMark(res: string, checked: boolean) {
    setMarked((prev) => {
      const next = checked ? [...prev, res] : prev.filter((x) => x !== res);
      try {
        window.localStorage.setItem("aMark", JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  }

  async function runAnalysis() {
    if (loadingRef.current) return;
    if (!sel.length) {
      toast("Pick at least one timeframe", "err");
      return;
    }
    const qs = new URLSearchParams({
      period: String(period),
      threshold: String(threshold),
      overbought: String(overbought),
      oversold: String(oversold),
    });
    TFS.filter((t) => sel.includes(t)).forEach((r) => qs.append("resolutions", r));
    const key = symbol + "?" + qs.toString();
    loadingRef.current = true;
    setLoading(true);
    try {
      const data = await api<AnalysisResponse>("GET", `/analysis/${symbol}?${qs.toString()}`);
      setState((prev) => pushSnapshot(prev, key, data, Date.now()));
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Failed to load analysis";
      toast(msg, "err");
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }

  // Re-fetch whenever symbol/selection/params change. Waits for `ready` so the
  // very first fetch uses the selection loaded from localStorage, not the
  // placeholder default.
  useEffect(() => {
    if (!ready) return;
    runAnalysis();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, symbol, sel, period, threshold, overbought, oversold]);

  // Auto-refresh while visible.
  useEffect(() => {
    if (!ready || !autoRefresh) return;
    const id = setInterval(() => {
      if (!document.hidden) runAnalysis();
    }, every * 1000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, autoRefresh, every, symbol, sel, period, threshold, overbought, oversold]);

  function showSnap(i: number) {
    setState((prev) => {
      if (i < 0 || i >= prev.snaps.length) return prev;
      return { ...prev, idx: i };
    });
  }

  // Arrow-key navigation when no input is focused.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const tag = (document.activeElement?.tagName ?? "").toUpperCase();
      if (/INPUT|SELECT|TEXTAREA/.test(tag)) return;
      if (e.key === "ArrowLeft") showSnap(stateRef.current.idx - 1);
      else if (e.key === "ArrowRight") showSnap(stateRef.current.idx + 1);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  const { snaps, idx } = state;
  const snap = idx >= 0 && idx < snaps.length ? snaps[idx] : null;
  const prevSnap = idx > 0 ? snaps[idx - 1] : null;
  const a = snap?.data as AnalysisResponse | undefined;

  const dlt = (cur: number | null | undefined, old: number | null | undefined, d = 2) =>
    old == null || cur == null || cur === old ? "" : ` (${cur > old ? "▲" : "▼"}${num(Math.abs(cur - old), d)})`;

  const priceCls = prevSnap && a && a.price !== prevSnap.data.price ? (a.price > prevSnap.data.price ? "g" : "r") : "";
  const oi = a?.oi_change_usd_6h;
  const oiCls = oi != null ? (oi > 0 ? "g" : oi < 0 ? "r" : "d") : "";

  const live = idx === snaps.length - 1;
  const stamp = snaps.length
    ? (live ? "live · updated " : "snapshot from ") +
      new Date(snaps[idx].time).toLocaleTimeString() +
      (live ? "" : ` (${Math.round((Date.now() - snaps[idx].time) / 1000)}s ago)`)
    : "";

  return (
    <div>
      <section>
        <h2>Analysis</h2>
        <div className="chips">
          {TFS.map((t) => (
            <Chip key={t} on={sel.includes(t)} onClick={() => toggleTf(t)}>
              {t}
            </Chip>
          ))}
          <button type="button" className="chip all" onClick={toggleAll}>
            {sel.length === TFS.length ? "Clear" : "All"}
          </button>
        </div>
        <div className="autoRow">
          <label>
            Period
            <input type="number" min={2} style={{ width: 70 }} value={period} onChange={(e) => setPeriod(+e.target.value)} />
          </label>
          <label>
            ADX threshold
            <input type="number" style={{ width: 70 }} value={threshold} onChange={(e) => setThreshold(+e.target.value)} />
          </label>
          <label>
            RSI overbought
            <input type="number" style={{ width: 70 }} value={overbought} onChange={(e) => setOverbought(+e.target.value)} />
          </label>
          <label>
            oversold
            <input type="number" style={{ width: 70 }} value={oversold} onChange={(e) => setOversold(+e.target.value)} />
          </label>
          <button type="button" className="btn" onClick={() => runAnalysis()} disabled={loading}>
            {loading ? "Refreshing…" : "⟳ Refresh now"}
          </button>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            auto-refresh every
            <select
              value={every}
              onChange={(e) => {
                const v = +e.target.value;
                setEvery(v);
                try {
                  window.localStorage.setItem("aEvery", String(v));
                } catch {
                  // ignore
                }
              }}
            >
              <option value={5}>5 sec</option>
              <option value={10}>10 sec</option>
              <option value={20}>20 sec</option>
              <option value={30}>30 sec</option>
              <option value={60}>1 min</option>
              <option value={120}>2 min</option>
              <option value={300}>5 min</option>
            </select>
          </label>
        </div>
        <div className="autoRow" style={{ marginTop: 10 }}>
          <button type="button" className="btn ghost sm" title="Older snapshot (←)" disabled={idx <= 0} onClick={() => showSnap(idx - 1)}>
            {"◀"}
          </button>
          <b style={{ minWidth: 48, textAlign: "center" }}>{snaps.length ? `${idx + 1} / ${snaps.length}` : "–"}</b>
          <button
            type="button"
            className="btn ghost sm"
            title="Newer snapshot (→)"
            disabled={idx >= snaps.length - 1}
            onClick={() => showSnap(idx + 1)}
          >
            {"▶"}
          </button>
          <button type="button" className="btn ghost sm" disabled={live || !snaps.length} onClick={() => showSnap(snaps.length - 1)}>
            Latest
          </button>
          <span className="dots">
            {snaps.map((s, i) => (
              <button
                key={s.time + "-" + i}
                type="button"
                className={"dot" + (i === idx ? " on" : "")}
                title={new Date(s.time).toLocaleTimeString()}
                onClick={() => showSnap(i)}
              />
            ))}
          </span>
          <span className="muted">{stamp}</span>
        </div>
        {a && (
          <div className="stats">
            <Stat label="Symbol" value={a.symbol} />
            <Stat label="Price (USD)" value={num(a.price) + dlt(a.price, prevSnap?.data.price)} cls={priceCls} />
            <Stat label="Leverage" value={`${a.leverage}x`} />
            <Stat label="Max contracts" value={a.max_contracts} />
            <Stat label="24h volume" value={num(a.volume, 2)} />
            <Stat label="Open interest" value={`${num(a.oi_contracts, 0)} ($${num(a.oi_value_usd, 0)})`} />
            <Stat label="OI change 6h" value={oi == null ? "–" : `${oi > 0 ? "+" : ""}${num(oi, 0)}`} cls={oiCls} />
          </div>
        )}
        {a && <AnalysisTable timeframes={a.timeframes} marked={marked} onToggleMark={toggleMark} />}
      </section>
    </div>
  );
}
