"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { type ChatMessage, type ThinkingEvent, parseSseLine, groupEventsByNode, formatTokenStream } from "@/lib/aiChat";

function isErrorMessage(content: string): boolean {
  return /^(something went wrong|error|skipped \()/i.test(content.trim());
}

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function AiChatPage() {
  const toast = useToast();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [liveEvents, setLiveEvents] = useState<ThinkingEvent[]>([]);
  const esRef = useRef<EventSource | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  async function loadMessages() {
    try {
      setMessages(await api<ChatMessage[]>("GET", "/ai/chat/messages?limit=100"));
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to load chat history", "err");
    }
  }

  useEffect(() => {
    loadMessages();
    return () => esRef.current?.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, liveEvents]);

  async function send() {
    const message = input.trim();
    if (!message || sending) return;
    setInput("");
    setSending(true);
    setLiveEvents([]);
    setMessages((prev) => [
      ...prev,
      { id: -Date.now(), ts: Math.floor(Date.now() / 1000), role: "user", content: message, signal_id: null },
    ]);
    try {
      await api("POST", "/ai/chat", { message });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to send", "err");
      setSending(false);
      loadMessages();
      return;
    }
    const es = new EventSource("/api/ai/chat/stream");
    esRef.current = es;
    es.onmessage = (ev) => {
      const parsed = parseSseLine(`data: ${ev.data}`);
      if (!parsed) return;
      setLiveEvents((prev) => [...prev, parsed]);
      if (parsed.type === "done" || parsed.type === "error") {
        es.close();
        esRef.current = null;
        setSending(false);
        loadMessages();
      }
    };
    es.onerror = () => {
      es.close();
      esRef.current = null;
      setSending(false);
      loadMessages();
    };
  }

  const grouped = groupEventsByNode(liveEvents);
  const dataAgentCalls = (grouped.data_agent ?? [])
    .filter((e) => e.type === "tool_call")
    .map((e) => `${e.name}(${Object.values(e.args ?? {}).join(", ")})`);
  const coordinatorText = formatTokenStream(grouped.coordinator ?? []);
  const executerText = formatTokenStream(grouped.executer ?? []);
  const guardrailEvents = (grouped.executer ?? []).filter((e) => e.type === "guardrail_result");

  return (
    <div>
      <section style={{ maxWidth: 760, margin: "0 auto", padding: 0, overflow: "hidden" }}>
        <div style={{ padding: "14px 18px", borderBottom: "1px solid var(--line)", display: "flex", alignItems: "center", gap: 10 }}>
          <a href="/ai-trading" className="btn ghost sm">
            ← AI Trading
          </a>
          <h2 style={{ margin: 0 }}>Chat with the coordinator</h2>
        </div>

        <div
          ref={scrollRef}
          style={{
            height: "56vh",
            minHeight: 320,
            overflowY: "auto",
            padding: "16px 18px",
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          {messages.length === 0 && (
            <p className="muted" style={{ textAlign: "center", marginTop: 40 }}>
              Ask a question about the market, or tell the coordinator what to do — e.g. "go long BTC on the 15m".
            </p>
          )}
          {messages.map((m) => {
            const isUser = m.role === "user";
            const isError = !isUser && isErrorMessage(m.content);
            return (
              <div
                key={m.id}
                style={{
                  alignSelf: isUser ? "flex-end" : "flex-start",
                  maxWidth: "82%",
                  display: "flex",
                  flexDirection: "column",
                  gap: 3,
                  alignItems: isUser ? "flex-end" : "flex-start",
                }}
              >
                <div
                  style={{
                    background: isUser ? "var(--accent)" : isError ? "color-mix(in srgb, var(--red) 16%, var(--panel))" : "var(--bg)",
                    border: isUser ? "none" : `1px solid ${isError ? "var(--red)" : "var(--line)"}`,
                    color: isUser ? "#fff" : "var(--text)",
                    padding: "9px 13px",
                    borderRadius: 10,
                    borderBottomRightRadius: isUser ? 2 : 10,
                    borderBottomLeftRadius: isUser ? 10 : 2,
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                  }}
                >
                  {m.signal_id != null && (
                    <span className="tag g" style={{ marginRight: 6, verticalAlign: "middle" }}>
                      trade
                    </span>
                  )}
                  {m.content}
                </div>
                <small className="muted" style={{ fontSize: 11 }}>
                  {formatTime(m.ts)}
                </small>
              </div>
            );
          })}

          {sending && (
            <div
              style={{
                alignSelf: "flex-start",
                maxWidth: "90%",
                width: "100%",
                background: "var(--bg)",
                border: "1px solid var(--line)",
                borderRadius: 10,
                padding: "10px 13px",
                fontSize: 13,
              }}
            >
              <div className="muted" style={{ marginBottom: 6, fontSize: 11, textTransform: "uppercase", letterSpacing: ".05em" }}>
                Live agent thinking
              </div>

              <div style={{ display: "grid", gap: 8 }}>
                <ThinkingRow label="data_agent" active={!coordinatorText && !executerText}>
                  {dataAgentCalls.length > 0 ? dataAgentCalls.join(", ") : "gathering context…"}
                </ThinkingRow>
                <ThinkingRow label="coordinator" active={!!coordinatorText && !executerText}>
                  {coordinatorText || "thinking…"}
                </ThinkingRow>
                {(executerText || guardrailEvents.length > 0) && (
                  <ThinkingRow label="executer" active={!!executerText}>
                    {executerText}
                    {guardrailEvents.map((e, i) => (
                      <div key={i} className={e.allowed ? "g" : "r"} style={{ marginTop: 4, fontWeight: 600 }}>
                        {e.allowed ? "✓ guardrails passed" : `✕ rejected: ${e.reason}`}
                      </div>
                    ))}
                  </ThinkingRow>
                )}
              </div>
            </div>
          )}
        </div>

        <div style={{ borderTop: "1px solid var(--line)", padding: "12px 18px", display: "flex", gap: 8 }}>
          <input
            style={{ flex: 1 }}
            value={input}
            disabled={sending}
            placeholder="Ask a question, or give a trade instruction..."
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
          />
          <button type="button" className="btn" disabled={sending || !input.trim()} onClick={send}>
            {sending ? "Thinking…" : "Send"}
          </button>
        </div>
      </section>
    </div>
  );
}

function ThinkingRow({ label, active, children }: { label: string; active: boolean; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 2 }}>
        {active && (
          <span
            className="thinking-dot"
            style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--accent)", display: "inline-block" }}
          />
        )}
        <small className="muted" style={{ textTransform: "uppercase", letterSpacing: ".04em" }}>
          {label}
        </small>
      </div>
      <div style={{ color: "var(--text)" }}>{children}</div>
    </div>
  );
}
