"use client";
import { UserButton } from "@clerk/nextjs";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Nav } from "@/components/Nav";
import { SymbolProvider, useSymbol } from "@/lib/symbol";
import { api } from "@/lib/api";
import { num, signClass } from "@/lib/format";

function Header() {
  const [symbol, setSymbol] = useSymbol();
  const { data } = useQuery({
    queryKey: ["price", symbol],
    queryFn: () => api<{ price: number }>("GET", `/price/${symbol}`),
    refetchInterval: 5000,
  });

  return (
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
