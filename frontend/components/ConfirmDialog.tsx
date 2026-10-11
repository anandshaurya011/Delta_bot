"use client";
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

type ConfirmFn = (text: string) => Promise<boolean>;

const ConfirmCtx = createContext<ConfirmFn | null>(null);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [text, setText] = useState<string | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);
  const dialogRef = useRef<HTMLDialogElement | null>(null);

  const confirm = useCallback<ConfirmFn>((t) => {
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
      setText(t);
      requestAnimationFrame(() => dialogRef.current?.showModal());
    });
  }, []);

  const close = (result: boolean) => {
    dialogRef.current?.close();
    setText(null);
    resolver.current?.(result);
    resolver.current = null;
  };

  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      <dialog ref={dialogRef} onCancel={(e) => { e.preventDefault(); close(false); }}>
        {text != null && (
          <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
            <div>{text}</div>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn ghost" onClick={() => close(false)}>Cancel</button>
              <button className="btn" onClick={() => close(true)}>Confirm</button>
            </div>
          </div>
        )}
      </dialog>
    </ConfirmCtx.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmCtx);
  if (!ctx) throw new Error("useConfirm must be used within ConfirmProvider");
  return ctx;
}
