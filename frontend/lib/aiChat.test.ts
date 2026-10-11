// frontend/lib/aiChat.test.ts
import { expect, it } from "vitest";
import { parseSseLine, groupEventsByNode, formatTokenStream, type ThinkingEvent } from "./aiChat";

it("parses a well-formed SSE data line", () => {
  const event = parseSseLine('data: {"type":"token","node":"coordinator","text":"hi"}');
  expect(event).toEqual({ type: "token", node: "coordinator", text: "hi" });
});

it("returns null for a blank or non-data line", () => {
  expect(parseSseLine("")).toBeNull();
  expect(parseSseLine(": keep-alive")).toBeNull();
});

it("returns null for malformed JSON rather than throwing", () => {
  expect(parseSseLine("data: not json")).toBeNull();
});

it("groups events by node, with no-node events under _meta", () => {
  const events: ThinkingEvent[] = [
    { node: "data_agent", type: "tool_call", name: "get_position" },
    { node: "coordinator", type: "token", text: "a" },
    { node: "coordinator", type: "token", text: "b" },
    { type: "done" },
  ];
  const grouped = groupEventsByNode(events);
  expect(grouped.data_agent).toHaveLength(1);
  expect(grouped.coordinator).toHaveLength(2);
  expect(grouped._meta).toEqual([{ type: "done" }]);
});

it("formats a token stream by concatenating text in order, ignoring other event types", () => {
  const events: ThinkingEvent[] = [
    { node: "coordinator", type: "token", text: "hel" },
    { node: "data_agent", type: "tool_call", name: "get_position" },
    { node: "coordinator", type: "token", text: "lo" },
  ];
  expect(formatTokenStream(events)).toBe("hello");
});
