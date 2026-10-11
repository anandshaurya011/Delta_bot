"use client";
// Ported from backend/static/index.html lines 476-490 (loadDash, closePos).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { num } from "@/lib/format";
import { PositionsTable, type Position } from "@/components/PositionsTable";

type Balance = {
  asset_symbol: string;
  balance: number | string;
  available_balance: number | string;
  available_balance_inr: number | string;
};

export default function DashboardPage() {
  const qc = useQueryClient();

  const balancesQ = useQuery({
    queryKey: ["balances"],
    queryFn: () => api<Balance[]>("GET", "/balances"),
  });
  const positionsQ = useQuery({
    queryKey: ["positions"],
    queryFn: () => api<Position[]>("GET", "/positions"),
    refetchInterval: 10000,
  });

  const balances = (balancesQ.data ?? []).filter((b) => +b.balance !== 0);
  const positions = positionsQ.data ?? [];
  const totalPnl = positions.reduce((s, p) => s + (p.unrealized_pnl || 0), 0);

  function onClosed() {
    qc.invalidateQueries({ queryKey: ["positions"] });
    qc.invalidateQueries({ queryKey: ["balances"] });
  }

  return (
    <div>
      <section>
        <h2>Balances</h2>
        <div className="tablewrap">
          {balances.length === 0 ? (
            <div className="muted">No non-zero balances.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Asset</th>
                  <th>Balance</th>
                  <th>Available</th>
                  <th>Available (INR)</th>
                </tr>
              </thead>
              <tbody>
                {balances.map((b) => (
                  <tr key={b.asset_symbol}>
                    <td>{b.asset_symbol}</td>
                    <td>{b.balance}</td>
                    <td>{b.available_balance}</td>
                    <td>₹{num(b.available_balance_inr)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section>
        <h2>
          Open positions{" "}
          <span className="muted">{positions.length ? `· total unrealized ${num(totalPnl, 4)}` : ""}</span>
        </h2>
        <PositionsTable positions={positions} onClosed={onClosed} />
      </section>
    </div>
  );
}
