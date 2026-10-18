"use client";
import type { CSSProperties } from "react";
import { markColor } from "@/lib/analysis";
import { num } from "@/lib/format";
import { Tag } from "@/components/Ui";

type Timeframe = {
  resolution: string;
  error?: string;
  adx?: number;
  rsi?: number;
  atr?: number;
  rsi_zone?: string;
  is_trending?: boolean;
  direction?: string;
  structure?: string;
  plus_di?: number;
  minus_di?: number;
  volume?: number;
  volume_avg?: number | null;
  volume_ratio?: number | null;
};

const COLS = ["Timeframe", "ADX", "RSI", "ATR", "Zone", "Strength", "Direction", "Structure", "DI+", "DI-", "Volume", "Vol Avg", "Vol Ratio"];

export function AnalysisTable({
  timeframes,
  marked,
  onToggleMark,
}: {
  timeframes: Timeframe[];
  marked: string[];
  onToggleMark: (res: string, checked: boolean) => void;
}) {
  return (
    <div className="tablewrap">
      <table className="term">
        <thead>
          <tr>
            {COLS.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {timeframes.map((x) => {
            const isMarked = marked.includes(x.resolution);
            const style = { "--mc": markColor(x.resolution) } as CSSProperties;
            if (x.error) {
              return (
                <tr key={x.resolution} className={isMarked ? "marked" : ""} style={style}>
                  <td className="tfc">
                    <label>
                      <input
                        type="checkbox"
                        checked={isMarked}
                        onChange={(e) => onToggleMark(x.resolution, e.target.checked)}
                      />
                      {x.resolution}
                    </label>
                  </td>
                  <td colSpan={COLS.length - 1} className="d" style={{ textAlign: "left" }}>
                    {/need at least \d+ candles|resolution is supported only for/i.test(x.error)
                      ? "data is not present"
                      : `error: ${x.error}`}
                  </td>
                </tr>
              );
            }
            const zc = x.rsi_zone === "overbought" ? "r" : x.rsi_zone === "oversold" ? "g" : "d";
            const sc = ["HH", "HL"].includes(x.structure ?? "") ? "g" : ["LH", "LL"].includes(x.structure ?? "") ? "r" : "d";
            const upDi = (x.plus_di ?? 0) >= (x.minus_di ?? 0);
            return (
              <tr key={x.resolution} className={isMarked ? "marked" : ""} style={style}>
                <td className="tfc">
                  <label>
                    <input
                      type="checkbox"
                      checked={isMarked}
                      onChange={(e) => onToggleMark(x.resolution, e.target.checked)}
                    />
                    {x.resolution}
                  </label>
                </td>
                <td>{num(x.adx)}</td>
                <td>{num(x.rsi, 1)}</td>
                <td>{num(x.atr)}</td>
                <td style={{ textAlign: "center" }}>
                  <Tag text={x.rsi_zone ?? ""} cls={zc} />
                </td>
                <td style={{ textAlign: "center" }}>
                  <Tag text={x.is_trending ? "trending" : "ranging"} cls={x.is_trending ? "g" : "w"} />
                </td>
                <td style={{ textAlign: "center" }}>
                  <Tag text={(x.direction ?? "").toUpperCase()} cls={x.direction === "up" ? "g" : "r"} />
                </td>
                <td style={{ textAlign: "center" }}>
                  <Tag text={x.structure || "n/a"} cls={sc} />
                </td>
                <td className={upDi ? "g" : "d"}>{num(x.plus_di)}</td>
                <td className={upDi ? "d" : "r"}>{num(x.minus_di)}</td>
                <td>{num(x.volume)}</td>
                <td>{x.volume_avg == null ? "n/a" : num(x.volume_avg)}</td>
                <td className={(x.volume_ratio ?? 0) >= 1.5 ? "g" : ""}>
                  {x.volume_ratio == null ? "n/a" : `${num(x.volume_ratio)}x`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
