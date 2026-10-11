export type ChatMessage = {
  id: number;
  ts: number;
  role: "user" | "assistant";
  content: string;
  signal_id: number | null;
};

export type ThinkingEvent = {
  node?: "data_agent" | "coordinator" | "executer";
  type: "token" | "tool_call" | "guardrail_result" | "done" | "error";
  [key: string]: any;
};

export function parseSseLine(line: string): ThinkingEvent | null {
  if (!line.startsWith("data: ")) return null;
  const payload = line.slice("data: ".length).trim();
  if (!payload) return null;
  try {
    return JSON.parse(payload) as ThinkingEvent;
  } catch {
    return null;
  }
}

export function groupEventsByNode(events: ThinkingEvent[]): Record<string, ThinkingEvent[]> {
  const grouped: Record<string, ThinkingEvent[]> = {};
  for (const e of events) {
    const key = e.node ?? "_meta";
    (grouped[key] ??= []).push(e);
  }
  return grouped;
}

export function formatTokenStream(events: ThinkingEvent[]): string {
  return events
    .filter((e) => e.type === "token")
    .map((e) => e.text ?? "")
    .join("");
}

export type MarkdownBlock = { type: "p" | "ul" | "ol"; items: string[] };

// Deliberately tiny and safe: parses into a block tree (never raw HTML), so the
// renderer can build React nodes directly -- LLM output never passes through
// dangerouslySetInnerHTML. Supports only what model replies actually use: paragraphs,
// "-"/"•" bullet lists, "1." numbered lists, and **bold** inline spans.
export function parseLiteMarkdown(text: string): MarkdownBlock[] {
  const blocks = text.trim().split(/\n{2,}/).filter(Boolean);
  return blocks.map((block): MarkdownBlock => {
    const lines = block.split("\n").map((l) => l.trim()).filter(Boolean);
    if (lines.length > 0 && lines.every((l) => /^[-•]\s+/.test(l))) {
      return { type: "ul", items: lines.map((l) => l.replace(/^[-•]\s+/, "")) };
    }
    if (lines.length > 0 && lines.every((l) => /^\d+[.)]\s+/.test(l))) {
      return { type: "ol", items: lines.map((l) => l.replace(/^\d+[.)]\s+/, "")) };
    }
    return { type: "p", items: [lines.join(" ")] };
  });
}

export type InlineSpan = { bold: boolean; text: string };

export function parseInlineBold(text: string): InlineSpan[] {
  return text
    .split(/(\*\*[^*]+\*\*)/g)
    .filter((p) => p !== "")
    .map((p) => {
      const m = p.match(/^\*\*([^*]+)\*\*$/);
      return m ? { bold: true, text: m[1] } : { bold: false, text: p };
    });
}
