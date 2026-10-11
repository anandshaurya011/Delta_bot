import { describe, expect, it, vi, beforeEach } from "vitest";
import { api, ApiError, errorMessage } from "./api";
import { num } from "./format";

describe("errorMessage", () => {
  it("prefers delta_error then detail", () => {
    expect(errorMessage(502, { delta_error: { code: "x" } })).toContain('"code":"x"');
    expect(errorMessage(400, { detail: "bad" })).toBe("400: bad");
  });
});
describe("num", () => {
  it("formats and handles empties", () => { expect(num(1234.5)).toBe("1,234.50"); expect(num(null)).toBe("–"); expect(num("abc")).toBe("–"); });
});
describe("api", () => {
  beforeEach(() => { (globalThis as any).window = { location: { assign: vi.fn() } }; });
  it("redirects on 401 and 412 and throws ApiError", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 412, json: async () => ({ detail: "delta_credentials_missing" }) }) as any;
    await expect(api("GET", "/balances")).rejects.toBeInstanceOf(ApiError);
    expect((window as any).location.assign).toHaveBeenCalledWith("/settings?missing=1");
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) }) as any;
    await expect(api("GET", "/x")).rejects.toBeInstanceOf(ApiError);
    expect((window as any).location.assign).toHaveBeenCalledWith("/sign-in");
  });
  it("sends JSON body and returns parsed data", async () => {
    const f = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ a: 1 }) });
    globalThis.fetch = f as any;
    expect(await api("PUT", "/leverage/BTCUSD", { leverage: 5 })).toEqual({ a: 1 });
    expect(f).toHaveBeenCalledWith("/api/leverage/BTCUSD", expect.objectContaining({ method: "PUT", body: '{"leverage":5}' }));
  });
});
