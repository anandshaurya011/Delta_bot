import { describe, expect, it } from "vitest";
import { buildBackendUrl } from "./proxy";

describe("buildBackendUrl", () => {
  it("joins path segments and keeps the query string", () => {
    expect(buildBackendUrl(["orders", "BTCUSD", "12"], "?a=1", "http://x:8000")).toBe("http://x:8000/orders/BTCUSD/12?a=1");
  });
  it("encodes segments and refuses path traversal", () => {
    expect(buildBackendUrl(["a b"], "", "http://x")).toBe("http://x/a%20b");
    expect(() => buildBackendUrl(["..", "settings"], "", "http://x")).toThrow();
    expect(() => buildBackendUrl(["a", "%2e%2e"], "", "http://x")).toThrow();
  });
});
