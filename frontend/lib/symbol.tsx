"use client";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

const STORAGE_KEY = "symbol";
const DEFAULT_SYMBOL = "BTCUSD";

type SymbolCtx = [string, (s: string) => void];

const Ctx = createContext<SymbolCtx | null>(null);

export function SymbolProvider({ children }: { children: ReactNode }) {
  const [symbol, setSymbolState] = useState(DEFAULT_SYMBOL);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored) setSymbolState(stored);
    } catch {
      // ignore (private mode / disabled storage)
    }
  }, []);

  const setSymbol = useCallback((s: string) => {
    setSymbolState(s);
    try { window.localStorage.setItem(STORAGE_KEY, s); } catch { /* ignore */ }
  }, []);

  return <Ctx.Provider value={[symbol, setSymbol]}>{children}</Ctx.Provider>;
}

export function useSymbol(): SymbolCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSymbol must be used within SymbolProvider");
  return ctx;
}
