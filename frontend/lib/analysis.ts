export const TFS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "1d", "1w"] as const;
export const MARK_COLORS = [
  "#f59e0b", "#22d3ee", "#c084fc", "#f472b6", "#a3e635", "#fb7185",
  "#60a5fa", "#34d399", "#fbbf24", "#e879f9", "#2dd4bf", "#f87171",
];
export const markColor = (res: string) => MARK_COLORS[Math.max(0, TFS.indexOf(res as any)) % MARK_COLORS.length];
export const toggleSelected = (sel: string[], t: string) =>
  sel.includes(t) ? sel.filter((x) => x !== t) : TFS.filter((x) => sel.includes(x) || x === t);

export type Snap = { time: number; data: any };
export type SnapState = { snaps: Snap[]; idx: number; key: string };

export function pushSnapshot(st: SnapState, key: string, data: any, now: number, max = 10): SnapState {
  let { snaps, idx } = key !== st.key ? { snaps: [] as Snap[], idx: -1 } : st;
  const follow = idx === snaps.length - 1;
  snaps = [...snaps, { time: now, data }];
  if (snaps.length > max) {
    snaps = snaps.slice(1);
    if (!follow) idx = Math.max(0, idx - 1);
  }
  if (follow) idx = snaps.length - 1;
  return { snaps, idx, key };
}
