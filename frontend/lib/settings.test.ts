import { expect, it } from "vitest";
import { validateCredsForm } from "./settings";
it("validates", () => {
  expect(validateCredsForm({ apiKey: "", apiSecret: "x".repeat(8) })).toMatch(/key/i);
  expect(validateCredsForm({ apiKey: "abcd1234", apiSecret: "  " })).toMatch(/secret/i);
  expect(validateCredsForm({ apiKey: " abcd1234 ", apiSecret: "secret99" })).toBeNull();
});
