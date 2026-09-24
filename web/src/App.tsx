import { useEffect, useRef, useState } from "react";
import { createSession, decideApproval, getBootstrap, getSession, streamTurn, type SavedMessage, type SessionSummary, type TurnEvent } from "./api";
import { Composer, EmptyState, Header, Sidebar, Timeline, type Status, type TimelineItem } from "./components";

function savedItems(messages: SavedMessage[]): TimelineItem[] {
  return messages.flatMap<TimelineItem>((message, index) => {
    if (message.role === "tool") return [{ key: `saved-${index}`, kind: "tool" as const, name: message.name || "Tool", detail: "", result: message.content }];
    if ((message.role === "user" || message.role === "assistant") && message.content) {
      return [{ key: `saved-${index}`, kind: "message" as const, role: message.role, content: message.content }];
    }
    return [];
  });
}

export default function App() {
  const [token, setToken] = useState("");
  const [project, setProject] = useState("");
  const [model, setModel] = useState("");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [items, setItems] = useState<TimelineItem[]>([]);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [status, setStatus] = useState<Status>({ kind: "ready", message: "" });
  const loadVersion = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await getBootstrap();
        if (cancelled) return;
        setToken(data.token);
        setProject(data.project);
        setModel(data.model);
        setSessions(data.sessions);
        if (data.sessions[0]) {
          const history = await getSession(data.sessions[0].id);
          if (cancelled) return;
          setSelected(data.sessions[0].id);
          setItems(savedItems(history));
        }
      } catch (cause) {
        if (!cancelled) setStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [items]);

  useEffect(() => {
    const title = sessions.find((session) => session.id === selected)?.title || "New session";
    document.title = `${title} · Mini-Hermes`;
  }, [sessions, selected]);

  useEffect(() => {
    function escape(event: KeyboardEvent) { if (event.key === "Escape") setSidebarOpen(false); }
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, []);

  async function refreshSessions() {
    const data = await getBootstrap();
    setSessions(data.sessions);
  }

  async function openSession(id: string) {
    if (busy) return;
    const version = ++loadVersion.current;
    try {
      const history = await getSession(id);
      if (version !== loadVersion.current) return;
      setSelected(id);
      setItems(savedItems(history));
      setSidebarOpen(false);
      setStatus({ kind: "ready", message: "" });
    } catch (cause) {
      setStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
    }
  }

  async function newSession(): Promise<string | null> {
    if (busy || !token) return null;
    try {
      const id = await createSession(token);
      ++loadVersion.current;
      setSelected(id);
      setItems([]);
      setSessions((current) => [{ id, title: "New session", message_count: 0 }, ...current]);
      setStatus({ kind: "ready", message: "" });
      setSidebarOpen(false);
      requestAnimationFrame(() => document.getElementById("prompt")?.focus());
      return id;
    } catch (cause) {
      setStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
      return null;
    }
  }

  async function approve(id: string, allow: boolean) {
    await decideApproval(token, id, allow);
    setItems((current) => current.map((item) => item.kind === "approval" && item.id === id ? { ...item, decision: allow } : item));
    setStatus((current) => current.kind === "busy" && current.message === "Waiting for your decision"
      ? { kind: "busy", message: "Mini-Hermes is continuing…" } : current);
  }

  async function send() {
    const text = prompt.trim();
    if (!text || busy || !token) return;
    let sessionId = selected;
    if (!sessionId) sessionId = await newSession();
    if (!sessionId) return;
    setPrompt("");
    setItems((current) => [...current, { key: crypto.randomUUID(), kind: "message", role: "user", content: text }]);
    setBusy(true);
    setStatus({ kind: "busy", message: "Mini-Hermes is thinking…" });
    let currentTextKey: string | null = null;
    let finished = false;
    const toolKeys = new Map<string, string>();

    function onEvent(event: TurnEvent) {
      if (event.type === "delta") {
        if (!currentTextKey) {
          currentTextKey = crypto.randomUUID();
          setItems((current) => [...current, { key: currentTextKey!, kind: "message", role: "assistant", content: event.text }]);
        } else {
          const key = currentTextKey;
          setItems((current) => current.map((item) => item.key === key && item.kind === "message" ? { ...item, content: item.content + event.text } : item));
        }
      } else if (event.type === "tool_start") {
        currentTextKey = null;
        const key = crypto.randomUUID();
        toolKeys.set(event.id, key);
        setItems((current) => [...current, { key, kind: "tool", name: event.name, detail: event.detail, result: "" }]);
        setStatus({ kind: "busy", message: `Using ${event.name.replaceAll("_", " ")}…` });
      } else if (event.type === "tool_result") {
        const key = toolKeys.get(event.id);
        setItems((current) => current.map((item) => item.key === key && item.kind === "tool" ? { ...item, result: event.result || "No output" } : item));
        setStatus({ kind: "busy", message: "Mini-Hermes is working…" });
      } else if (event.type === "approval") {
        setItems((current) => [...current, { key: crypto.randomUUID(), kind: "approval", id: event.id, action: event.action, target: event.target, content: event.content }]);
        setStatus({ kind: "busy", message: "Waiting for your decision" });
      } else if (event.type === "done") {
        if (!currentTextKey && event.answer) {
          setItems((current) => [...current, { key: crypto.randomUUID(), kind: "message", role: "assistant", content: event.answer }]);
        }
        finished = true;
        setStatus({ kind: "ready", message: "" });
      } else if (event.type === "error") {
        finished = true;
        setStatus({ kind: "error", message: event.message });
      }
    }

    try {
      await streamTurn(token, sessionId, text, onEvent);
      if (!finished) throw new Error("The connection ended early. Reopen the session to inspect what was saved.");
    } catch (cause) {
      setStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
    } finally {
      setBusy(false);
      try { await refreshSessions(); }
      catch (cause) { setStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) }); }
      requestAnimationFrame(() => document.getElementById("prompt")?.focus());
    }
  }

  const title = sessions.find((session) => session.id === selected)?.title || "New session";
  return (
    <div className="app-shell">
      <Sidebar project={project} model={model} sessions={sessions} selected={selected} busy={busy} open={sidebarOpen} onClose={() => setSidebarOpen(false)} onNew={() => void newSession()} onSelect={(id) => void openSession(id)} />
      <main className="workspace">
        <Header title={title} model={model} status={status} sidebarOpen={sidebarOpen} onMenu={() => setSidebarOpen((open) => !open)} />
        <div id="chat-scroll" className="chat-scroll" ref={scrollRef}>
          {items.length === 0 ? <EmptyState /> : <Timeline items={items} onDecide={approve} />}
        </div>
        <Composer prompt={prompt} project={project} busy={busy || !token} status={status} onPrompt={setPrompt} onSend={() => void send()} />
      </main>
    </div>
  );
}
