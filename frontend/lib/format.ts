export const num = (v: unknown, d = 2): string =>
  v == null || v === "" || isNaN(+(v as number)) ? "–" : (+(v as number)).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
export const signClass = (v: number): "g" | "r" | "d" => (v > 0 ? "g" : v < 0 ? "r" : "d");
