// frontend/lib/aiChat.test.ts
import { expect, it } from "vitest";
import { parseSseLine, groupEventsByNode, formatTokenStream, parseLiteMarkdown, parseInlineBold, type ThinkingEvent } from "./aiChat";

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

it("parses a plain paragraph as a single p block", () => {
  expect(parseLiteMarkdown("BTC is trending up.")).toEqual([{ type: "p", items: ["BTC is trending up."] }]);
});

it("joins wrapped lines within one paragraph", () => {
  expect(parseLiteMarkdown("line one\nline two")).toEqual([{ type: "p", items: ["line one line two"] }]);
});

it("splits on blank lines into separate blocks", () => {
  expect(parseLiteMarkdown("first\n\nsecond")).toEqual([
    { type: "p", items: ["first"] },
    { type: "p", items: ["second"] },
  ]);
});

it("detects a bullet list block", () => {
  expect(parseLiteMarkdown("- buy small\n- watch volume")).toEqual([
    { type: "ul", items: ["buy small", "watch volume"] },
  ]);
});

it("detects a numbered list block", () => {
  expect(parseLiteMarkdown("1. check RSI\n2. check volume")).toEqual([
    { type: "ol", items: ["check RSI", "check volume"] },
  ]);
});

it("treats a mixed block (not every line a list marker) as a paragraph", () => {
  expect(parseLiteMarkdown("- buy small\nbut watch out")).toEqual([
    { type: "p", items: ["- buy small but watch out"] },
  ]);
});

it("parses inline bold spans without emitting raw HTML", () => {
  expect(parseInlineBold("go **long** now")).toEqual([
    { bold: false, text: "go " },
    { bold: true, text: "long" },
    { bold: false, text: " now" },
  ]);
});

it("returns a single plain span when there is no bold markup", () => {
  expect(parseInlineBold("plain text")).toEqual([{ bold: false, text: "plain text" }]);
});
