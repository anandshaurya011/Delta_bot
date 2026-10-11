export function Stat({ label, value, cls }: { label: string; value: string | number; cls?: string }) {
  return (
    <div className="stat">
      <small>{label}</small>
      <b className={cls}>{value}</b>
    </div>
  );
}

export function Tag({ text, cls }: { text: string; cls: string }) {
  return <span className={`tag ${cls}`}>{text}</span>;
}

export function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" className={`chip${on ? " on" : ""}`} onClick={onClick}>
      {children}
    </button>
  );
}
