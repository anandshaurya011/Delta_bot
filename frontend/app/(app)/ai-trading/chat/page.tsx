"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { type ChatMessage, type ThinkingEvent, parseSseLine, groupEventsByNode, formatTokenStream } from "@/lib/aiChat";

export default function AiChatPage() {
  const toast = useToast();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [liveEvents, setLiveEvents] = useState<ThinkingEvent[]>([]);
  const esRef = useRef<EventSource | null>(null);

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

  async function send() {
    const message = input.trim();
    if (!message || sending) return;
    setInput("");
    setSending(true);
    setLiveEvents([]);
    try {
      await api("POST", "/ai/chat", { message });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to send", "err");
      setSending(false);
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
    };
  }

  const grouped = groupEventsByNode(liveEvents);
  const coordinatorText = formatTokenStream(grouped.coordinator ?? []);

  return (
    <div>
      <section>
        <h2>Chat with the coordinator</h2>
        <div className="autoRow" style={{ flexDirection: "column", gap: 8, maxWidth: 700 }}>
          {messages.map((m) => (
            <div
              key={m.id}
              style={{
                alignSelf: m.role === "user" ? "flex-end" : "flex-start",
                background: m.role === "user" ? "#1e3a5f" : "#2a2a2a",
                padding: "8px 12px",
                borderRadius: 8,
                maxWidth: "80%",
              }}
            >
              {m.signal_id != null && <span className="tag" style={{ marginRight: 6 }}>trade</span>}
              {m.content}
            </div>
          ))}
        </div>

        {sending && (
          <div className="stats" style={{ marginTop: 12 }}>
            <div className="stat">
              <small>data_agent</small>
              <b>{(grouped.data_agent ?? []).map((e) => (e.type === "tool_call" ? `called ${e.name}` : "")).join(", ") || "…"}</b>
            </div>
            <div className="stat">
              <small>coordinator</small>
              <b>{coordinatorText || "…"}</b>
            </div>
            <div className="stat">
              <small>executer</small>
              <b>
                {(grouped.executer ?? [])
                  .filter((e) => e.type === "guardrail_result")
                  .map((e) => (e.allowed ? "guardrails passed" : `rejected: ${e.reason}`))
                  .join(", ") || "…"}
              </b>
            </div>
          </div>
        )}

        <div className="autoRow" style={{ marginTop: 12 }}>
          <input
            style={{ flex: 1 }}
            value={input}
            disabled={sending}
            placeholder="Ask a question, or give a trade instruction..."
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
          />
          <button type="button" className="btn" disabled={sending} onClick={send}>
            {sending ? "Thinking…" : "Send"}
          </button>
        </div>
      </section>
    </div>
  );
}
