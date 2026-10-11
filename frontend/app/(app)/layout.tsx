"use client";
import { UserButton } from "@clerk/nextjs";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Nav } from "@/components/Nav";
import { SymbolProvider, useSymbol } from "@/lib/symbol";
import { api } from "@/lib/api";
import { num, signClass } from "@/lib/format";

type HeaderPosition = {
  symbol: string;
  side: "long" | "short";
  size: number;
  entry_price: number;
  mark_price: number;
  unrealized_pnl: number;
};

function Header() {
  const [symbol, setSymbol] = useSymbol();
  const { data } = useQuery({
    queryKey: ["price", symbol],
    queryFn: () => api<{ price: number }>("GET", `/price/${symbol}`),
    refetchInterval: 5000,
  });
  // Ported from backend/static/index.html lines 372-386 (refreshPos, the posStrip markup).
  const posQ = useQuery({
    queryKey: ["positions"],
    queryFn: () => api<HeaderPosition[]>("GET", "/positions"),
    refetchInterval: 10000,
  });
  const pos = posQ.data ?? [];

  return (
    <>
      <header>
        <h1>Delta Trader</h1>
        <input
          value={symbol}
          onChange={(e) => setSymbol(e.target.value.toUpperCase())}
          style={{ width: 110 }}
          aria-label="symbol"
        />
        <span className={`price ${signClass(data?.price ?? 0)}`}>{num(data?.price)}</span>
        <div className="right">
          <UserButton />
        </div>
      </header>
      {pos.length > 0 && (
        <div style={{ padding: "8px 16px", borderBottom: "1px solid var(--line)", display: "flex", gap: 18, flexWrap: "wrap", fontVariantNumeric: "tabular-nums" }}>
          {pos.map((p) => (
            <span key={p.symbol}>
              <b>{p.symbol} </b>
              <span className={p.side === "long" ? "g" : "r"}>{p.side.toUpperCase()} {num(Math.abs(p.size), 0)}</span>
              {` @ ${num(p.entry_price)}  ·  mark ${num(p.mark_price)}  ·  P&L `}
              <b className={signClass(p.unrealized_pnl)}>{(p.unrealized_pnl > 0 ? "+" : "") + num(p.unrealized_pnl, 4)}</b>
            </span>
          ))}
        </div>
      )}
    </>
  );
}

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <SymbolProvider>
      <Header />
      <Nav />
      <main>{children}</main>
    </SymbolProvider>
  );
}
