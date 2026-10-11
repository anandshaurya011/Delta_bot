export function buildBackendUrl(path: string[], search: string, base: string): string {
  const segs = path.map((s) => {
    const d = decodeURIComponent(s);
    if (d === ".." || d === "." || d.includes("/")) throw new Error("bad path");
    return encodeURIComponent(d);
  });
  return `${base.replace(/\/$/, "")}/${segs.join("/")}${search}`;
}
