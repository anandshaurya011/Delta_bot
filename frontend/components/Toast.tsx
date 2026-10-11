"use client";
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

type Kind = "ok" | "err";
type ToastFn = (msg: string, kind?: Kind) => void;

const ToastCtx = createContext<ToastFn | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<{ msg: string; kind: Kind } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const show = useCallback<ToastFn>((msg, kind = "ok") => {
    setState({ msg, kind });
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState(null), kind === "err" ? 8000 : 3000);
  }, []);

  return (
    <ToastCtx.Provider value={show}>
      {children}
      {state && <div id="toast" className={state.kind}>{state.msg}</div>}
    </ToastCtx.Provider>
  );
}

export function useToast(): ToastFn {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
