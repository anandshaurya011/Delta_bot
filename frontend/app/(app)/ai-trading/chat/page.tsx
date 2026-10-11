"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { useSymbol } from "@/lib/symbol";
import {
  type ChatMessage,
  type ThinkingEvent,
  type MarkdownBlock,
  parseSseLine,
  groupEventsByNode,
  formatTokenStream,
  parseLiteMarkdown,
  parseInlineBold,
} from "@/lib/aiChat";

function isErrorMessage(content: string): boolean {
  return /^(something went wrong|error|skipped \()/i.test(content.trim());
}

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function Inline({ text }: { text: string }) {
  return (
    <>
      {parseInlineBold(text).map((span, i) =>
        span.bold ? <strong key={i}>{span.text}</strong> : <span key={i}>{span.text}</span>
      )}
    </>
  );
}

function Markdown({ text }: { text: string }) {
  const blocks: MarkdownBlock[] = parseLiteMarkdown(text);
  return (
    <>
      {blocks.map((block, i) => {
        if (block.type === "ul") {
          return (
            <ul key={i} style={{ margin: "4px 0", paddingLeft: 20 }}>
              {block.items.map((item, j) => (
                <li key={j}>
                  <Inline text={item} />
                </li>
              ))}
            </ul>
          );
        }
        if (block.type === "ol") {
          return (
            <ol key={i} style={{ margin: "4px 0", paddingLeft: 20 }}>
              {block.items.map((item, j) => (
                <li key={j}>
                  <Inline text={item} />
                </li>
              ))}
            </ol>
          );
        }
        return (
          <p key={i} style={{ margin: i === 0 ? 0 : "8px 0 0" }}>
            <Inline text={block.items[0]} />
          </p>
        );
      })}
    </>
  );
}

function Avatar({ role }: { role: "user" | "assistant" }) {
  return (
    <div
      style={{
        width: 28,
        height: 28,
        borderRadius: "50%",
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: 12,
        fontWeight: 700,
        background: role === "user" ? "var(--accent)" : "var(--panel)",
        border: role === "user" ? "none" : "1px solid var(--line)",
        color: role === "user" ? "#fff" : "var(--dim)",
      }}
    >
      {role === "user" ? "U" : "AI"}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const toast = useToast();
  return (
    <button
      type="button"
      className="btn ghost sm"
      style={{ padding: "2px 7px", fontSize: 11 }}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          toast("Copied", "ok");
        } catch {
          toast("Copy failed", "err");
        }
      }}
    >
      Copy
    </button>
  );
}

export default function AiChatPage() {
  const toast = useToast();
  const [symbol] = useSymbol();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [liveEvents, setLiveEvents] = useState<ThinkingEvent[]>([]);
  const esRef = useRef<EventSource | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

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

  function autoGrow() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }

  function stopGenerating() {
    esRef.current?.close();
    esRef.current = null;
    setSending(false);
    loadMessages();
  }

  async function send() {
    const message = input.trim();
    if (!message || sending) return;
    setInput("");
    requestAnimationFrame(autoGrow);
    setSending(true);
    setLiveEvents([]);
    setMessages((prev) => [
      ...prev,
      { id: -Date.now(), ts: Math.floor(Date.now() / 1000), role: "user", content: message, signal_id: null },
    ]);
    try {
      await api("POST", "/ai/chat", { message, symbol });
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

  const SUGGESTIONS = ["What's the current price?", "Analyse the market and suggest a trade", "What's my open position?"];

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "calc(100dvh - 150px)",
        minHeight: 480,
        maxWidth: 840,
        margin: "0 auto",
        background: "var(--panel)",
        border: "1px solid var(--line)",
        borderRadius: 10,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "12px 18px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexShrink: 0,
        }}
      >
        <a href="/ai-trading" className="btn ghost sm">
          ← Settings
        </a>
        <div>
          <div style={{ fontWeight: 600, fontSize: 14 }}>AI Trading Agent</div>
          <div className="muted" style={{ fontSize: 11 }}>
            coordinator · data_agent · executer
          </div>
        </div>
        <span className="tag d" style={{ marginLeft: "auto" }}>
          context: {symbol}
        </span>
        {messages.length > 0 && (
          <button type="button" className="btn ghost sm" onClick={() => setMessages([])} title="Clear this view (history stays on the server)">
            Clear
          </button>
        )}
      </div>

      <div
        ref={scrollRef}
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "20px 18px",
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        {messages.length === 0 && (
          <div style={{ textAlign: "center", marginTop: 40 }}>
            <p className="muted">Ask a question about the market, or tell the agent what to do.</p>
            <div className="autoRow" style={{ justifyContent: "center", marginTop: 8 }}>
              {SUGGESTIONS.map((s) => (
                <button key={s} type="button" className="chip" onClick={() => setInput(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m) => {
          const isUser = m.role === "user";
          const isError = !isUser && isErrorMessage(m.content);
          return (
            <div key={m.id} style={{ display: "flex", gap: 10, flexDirection: isUser ? "row-reverse" : "row" }}>
              <Avatar role={m.role} />
              <div style={{ display: "flex", flexDirection: "column", gap: 3, alignItems: isUser ? "flex-end" : "flex-start", maxWidth: "78%" }}>
                <div
                  className="chat-bubble"
                  style={{
                    background: isUser ? "var(--accent)" : isError ? "color-mix(in srgb, var(--red) 16%, var(--panel))" : "var(--bg)",
                    border: isUser ? "none" : `1px solid ${isError ? "var(--red)" : "var(--line)"}`,
                    color: isUser ? "#fff" : "var(--text)",
                    padding: "10px 14px",
                    borderRadius: 12,
                    borderTopRightRadius: isUser ? 3 : 12,
                    borderTopLeftRadius: isUser ? 12 : 3,
                    wordBreak: "break-word",
                  }}
                >
                  {m.signal_id != null && (
                    <span className="tag g" style={{ marginRight: 6, verticalAlign: "middle" }}>
                      trade
                    </span>
                  )}
                  {isUser ? m.content : <Markdown text={m.content} />}
                </div>
                <div className="autoRow" style={{ margin: 0, gap: 8 }}>
                  <small className="muted" style={{ fontSize: 11 }}>
                    {formatTime(m.ts)}
                  </small>
                  {!isUser && <CopyButton text={m.content} />}
                </div>
              </div>
            </div>
          );
        })}

        {sending && (
          <div style={{ display: "flex", gap: 10 }}>
            <Avatar role="assistant" />
            <div
              style={{
                maxWidth: "82%",
                width: "100%",
                background: "var(--bg)",
                border: "1px solid var(--line)",
                borderRadius: 12,
                borderTopLeftRadius: 3,
                padding: "10px 14px",
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
                  {coordinatorText ? <Markdown text={coordinatorText} /> : "thinking…"}
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
          </div>
        )}
      </div>

      <div style={{ borderTop: "1px solid var(--line)", padding: "12px 18px", flexShrink: 0 }}>
        <div className="autoRow" style={{ margin: 0, alignItems: "flex-end", gap: 8 }}>
          <textarea
            ref={textareaRef}
            rows={1}
            style={{
              flex: 1,
              resize: "none",
              maxHeight: 160,
              padding: "9px 12px",
              lineHeight: 1.4,
              fontFamily: "inherit",
            }}
            value={input}
            disabled={sending}
            placeholder="Ask a question, or give a trade instruction..."
            onChange={(e) => {
              setInput(e.target.value);
              autoGrow();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          {sending ? (
            <button type="button" className="btn ghost" onClick={stopGenerating}>
              Stop
            </button>
          ) : (
            <button type="button" className="btn" disabled={!input.trim()} onClick={send}>
              Send
            </button>
          )}
        </div>
      </div>
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
