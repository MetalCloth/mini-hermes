import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ArrowUp, Bot, ChevronDown, FileText, Menu, MessageSquare, Plus, Search, Sparkles, Wrench, X } from "lucide-react";
import type { SessionSummary } from "./api";

export type TimelineItem =
  | { key: string; kind: "message"; role: "user" | "assistant"; content: string }
  | { key: string; kind: "tool"; name: string; detail: string; result: string }
  | { key: string; kind: "approval"; id: string; action: string; target: string; content: string; decision?: boolean };

export type Status = { kind: "ready" | "busy" | "error"; message: string };

type SidebarProps = {
  project: string;
  model: string;
  sessions: SessionSummary[];
  selected: string | null;
  busy: boolean;
  open: boolean;
  onClose: () => void;
  onNew: () => void;
  onSelect: (id: string) => void;
};

export function Sidebar({ project, model, sessions, selected, busy, open, onClose, onNew, onSelect }: SidebarProps) {
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const matches = sessions.filter((session) => session.title.toLowerCase().includes(query.toLowerCase()));

  return (
    <aside id="sidebar" className={`sidebar${open ? " is-open" : ""}`} aria-label="Sessions">
      <div className="window-row">
        <div className="window-brand" aria-label="Mini-Hermes">mini<span>hermes</span><b>.</b></div>
        <button className="chrome-button search-button" type="button" aria-label="Find a session" onClick={() => searchRef.current?.focus()}><Search size={20} /></button>
        <button className="chrome-button sidebar-close" type="button" aria-label="Close sessions" onClick={onClose}><X size={20} /></button>
      </div>

      <div className="sidebar-scroll">
        <nav className="sidebar-nav" aria-label="Workspace navigation">
          <button id="new-session" className="nav-row" type="button" disabled={busy} onClick={onNew}>
            <Bot size={20} strokeWidth={1.7} /><span>New session</span><Plus className="nav-end" size={16} />
          </button>
          <button className="nav-row" type="button" onClick={() => searchRef.current?.focus()}>
            <MessageSquare size={20} strokeWidth={1.7} /><span>Sessions</span><span className="nav-count">{sessions.length}</span>
          </button>
        </nav>

        <section className="session-section" aria-label="Saved sessions">
          <div className="section-heading">RECENT SESSIONS</div>
          <label className="search-wrap">
            <span className="sr-only">Search saved sessions</span>
            <Search size={16} aria-hidden="true" />
            <input ref={searchRef} id="session-search" type="search" placeholder="Search sessions" value={query} onChange={(event) => setQuery(event.target.value)} />
          </label>
          <nav className="session-list" aria-label="Saved sessions">
            {matches.map((session) => (
              <button key={session.id} className="session-item" type="button" disabled={busy} aria-current={selected === session.id ? "page" : undefined} onClick={() => onSelect(session.id)}>
                <MessageSquare size={16} strokeWidth={1.7} aria-hidden="true" />
                <span className="session-copy"><span className="session-title">{session.title}</span><small>{session.message_count} {session.message_count === 1 ? "message" : "messages"}</small></span>
              </button>
            ))}
          </nav>
          {query && matches.length === 0 && <p className="sidebar-empty">No sessions match your search.</p>}
        </section>
      </div>

      <div className="sidebar-footer">
        <div className="footer-project"><span className="local-dot" aria-hidden="true" /> <span title={project}>{project.split("/").filter(Boolean).pop() || project || "Local project"}</span></div>
        <div className="footer-model"><span>LOCAL WORKSPACE</span><span>{model}</span></div>
      </div>
    </aside>
  );
}

type HeaderProps = {
  title: string;
  model: string;
  status: Status;
  sidebarOpen: boolean;
  onMenu: () => void;
};

export function Header({ title, model, status, sidebarOpen, onMenu }: HeaderProps) {
  return (
    <header className="topbar">
      <div className="topbar-left">
        <button id="sidebar-toggle" className="chrome-button menu-button" type="button" aria-label="Open sessions" aria-controls="sidebar" aria-expanded={sidebarOpen} onClick={onMenu}><Menu size={20} /></button>
        <span className="topbar-title">{title}</span><ChevronDown className="title-chevron" size={17} aria-hidden="true" />
      </div>
      <div className="topbar-right">
        <span className="header-model">{model}</span>
        <span id="connection-status" className={`connection-status ${status.kind}`}><span className="local-dot" aria-hidden="true" />{status.kind === "busy" ? "Working" : status.kind === "error" ? "Error" : "Ready"}</span>
      </div>
    </header>
  );
}

export function EmptyState() {
  return (
    <section className="empty-state" aria-label="Start a conversation">
      <h1 className="hero-wordmark"><span>MINI</span> HERMES</h1>
      <p>Describe the task in your own words. I’ll read the project, use the right tools, and check with you before changing files or running commands.</p>
    </section>
  );
}

function ApprovalCard({ item, onDecide }: { item: Extract<TimelineItem, { kind: "approval" }>; onDecide: (id: string, allow: boolean) => Promise<void> }) {
  const denyRef = useRef<HTMLButtonElement>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { if (item.decision === undefined) denyRef.current?.focus(); }, [item.id, item.decision]);

  async function decide(allow: boolean) {
    setSubmitting(true);
    setError("");
    try { await onDecide(item.id, allow); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setSubmitting(false); }
  }

  const title = item.action === "terminal" ? "Run a terminal command?" : `${item.action === "create" ? "Create" : "Replace"} ${item.target}?`;
  return (
    <section className="approval-card" aria-label="Tool approval requested">
      <div className="approval-label">APPROVAL REQUIRED</div>
      <h2>{title}</h2>
      <p>{item.decision === undefined ? "Review this action before Mini-Hermes continues." : item.decision ? "Approved. Mini-Hermes is continuing." : "Denied. Mini-Hermes is continuing without this action."}</p>
      <pre>{item.content}</pre>
      {error && <p className="approval-error" role="alert">{error}</p>}
      {item.decision === undefined && <div className="approval-actions">
        <button ref={denyRef} type="button" disabled={submitting} onClick={() => decide(false)}>Deny</button>
        <button className="allow-button" type="button" disabled={submitting} onClick={() => decide(true)}>Allow once</button>
      </div>}
    </section>
  );
}

export function Timeline({ items, onDecide }: { items: TimelineItem[]; onDecide: (id: string, allow: boolean) => Promise<void> }) {
  return (
    <div id="messages" className="messages" role="log" aria-label="Conversation" aria-live="polite" aria-relevant="additions text">
      {items.map((item) => {
        if (item.kind === "message") return (
          <article key={item.key} className={`message ${item.role}`}>
            <div className="message-avatar" aria-hidden="true">{item.role === "user" ? "YOU" : <Sparkles size={17} />}</div>
            <div className="message-body"><div className="message-role">{item.role === "user" ? "YOU" : "MINI-HERMES"}</div><div className="message-content">{item.content}</div></div>
          </article>
        );
        if (item.kind === "tool") return (
          <details key={item.key} className="tool-card">
            <summary><Wrench size={16} aria-hidden="true" /><span>{item.name.replaceAll("_", " ")}</span><small>{item.detail}</small></summary>
            <pre>{item.result || "Running…"}</pre>
          </details>
        );
        return <ApprovalCard key={item.key} item={item} onDecide={onDecide} />;
      })}
    </div>
  );
}

type ComposerProps = {
  prompt: string;
  project: string;
  busy: boolean;
  status: Status;
  onPrompt: (value: string) => void;
  onSend: () => void;
};

export function Composer({ prompt, project, busy, status, onPrompt, onSend }: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const area = textareaRef.current;
    if (area) { area.style.height = "auto"; area.style.height = `${Math.min(area.scrollHeight, 150)}px`; }
  }, [prompt]);
  function submit(event: FormEvent) { event.preventDefault(); if (!busy && prompt.trim()) onSend(); }
  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }
  return (
    <div className="composer-area">
      <p className={`status-message ${status.kind}`} role="status" aria-live="polite">{status.message}</p>
      <form className="composer" onSubmit={submit}>
        <label htmlFor="prompt" className="sr-only">Message Mini-Hermes</label>
        <textarea ref={textareaRef} id="prompt" rows={1} placeholder="Send a message…" maxLength={10000} required disabled={busy} value={prompt} onChange={(event) => onPrompt(event.target.value)} onKeyDown={keyDown} />
        <div className="composer-bottom">
          <span className="composer-note"><FileText size={14} aria-hidden="true" /> Working in <strong>{project.split("/").filter(Boolean).pop() || project || "your project"}</strong></span>
          <span className="composer-actions"><span className="keyboard-hint">Enter to send · Shift+Enter for a new line</span><button id="send-button" className="send-button" type="submit" aria-label="Send message" disabled={busy || !prompt.trim()}><ArrowUp size={18} strokeWidth={2.5} /></button></span>
        </div>
      </form>
      <div className="composer-footnote">Mini-Hermes asks before writing files or running commands.</div>
    </div>
  );
}
