import { describe, expect, it } from "vitest";
import { TFS, markColor, toggleSelected, pushSnapshot } from "./analysis";

it("colours are stable per timeframe and distinct", () => {
  expect(markColor("15m")).toBe(markColor("15m"));
  expect(new Set(TFS.map(markColor)).size).toBe(TFS.length);
});
it("toggleSelected keeps canonical order", () => {
  expect(toggleSelected(["1h", "5m"], "15m")).toEqual(["5m", "15m", "1h"]);
  expect(toggleSelected(["5m", "1h"], "5m")).toEqual(["1h"]);
});
describe("pushSnapshot", () => {
  const s0 = { snaps: [], idx: -1, key: "" };
  it("follows latest and caps at 10", () => {
    let s = s0 as any;
    for (let i = 0; i < 12; i++) s = pushSnapshot(s, "k", { i }, i);
    expect(s.snaps.length).toBe(10); expect(s.idx).toBe(9); expect(s.snaps[0].data.i).toBe(2);
  });
  it("does not jump when viewing an older snapshot", () => {
    let s = s0 as any;
    for (let i = 0; i < 3; i++) s = pushSnapshot(s, "k", { i }, i);
    s = { ...s, idx: 0 };
    s = pushSnapshot(s, "k", { i: 3 }, 3);
    expect(s.idx).toBe(0);
  });
  it("resets when the key changes", () => {
    let s = pushSnapshot(s0 as any, "a", {}, 0); s = pushSnapshot(s, "b", {}, 1);
    expect(s.snaps.length).toBe(1); expect(s.key).toBe("b");
  });
});
