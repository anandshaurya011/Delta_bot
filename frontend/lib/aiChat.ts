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
