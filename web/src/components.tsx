import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";
import { Bot, ChevronDown, FileCode2, Folder, Menu, MessageSquare, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Pencil, Plus, Search, Settings2, Trash2, Wrench, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import type { McpServerStatus, SessionSummary } from "./api";
import mascotUrl from "./assets/mascot.png";
import { ThinkingOrb, type OrbState } from "@/components/ui/thinking-orbs";
import { PromptInputBox, type ShortcutId } from "@/components/ui/ai-prompt-box";
import { normalizeLatexDelimiters } from "./markdown";

export type TimelineItem =
  | { key: string; kind: "message"; role: "user" | "assistant"; content: string; turnStatus?: "cancelled" | "failed" }
  | { key: string; kind: "tool"; name: string; detail: string; result: string }
  | { key: string; kind: "approval"; id: string; action: string; target: string; content: string; decision?: boolean };

export type Status = { kind: "ready" | "busy" | "error"; message: string };

const MIN_SIDEBAR_WIDTH = 220;
const MAX_SIDEBAR_WIDTH = 420;

function maxSidebarWidth() {
  return Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, window.innerWidth - 320));
}

function Mascot({ size = 30 }: { size?: number }) {
  return <span className="mascot" style={{ width: size, height: size }} role="img" aria-label="Oryn mascot"><img src={mascotUrl} alt="" /></span>;
}

type SidebarProps = {
  project: string;
  projects: string[];
  model: string;
  sessions: SessionSummary[];
  selected: string | null;
  running: string[];
  open: boolean;
  expanded: boolean;
  onClose: () => void;
  onExpand: () => void;
  onNew: () => void;
  onSelect: (id: string) => void;
  onRename: (id: string) => void;
  onDelete: (id: string) => void;
  onProject: (root: string) => void;
};

export function Sidebar({ project, projects, model, sessions, selected, running, open, expanded, onClose, onExpand, onNew, onSelect, onRename, onDelete, onProject }: SidebarProps) {
  const [query, setQuery] = useState("");
  const [collapsedProjects, setCollapsedProjects] = useState<string[]>([]);
  const [width, setWidth] = useState(() => window.innerWidth <= 980 ? 280 : Math.round(Math.min(352, Math.max(270, window.innerWidth * .254))));
  const [resizing, setResizing] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const term = query.trim().toLowerCase();
  const matches = projects.map((root) => {
    const name = root.split("/").filter(Boolean).pop() || root;
    const folderMatches = name.toLowerCase().includes(term) || root.toLowerCase().includes(term);
    const chats = sessions.filter((session) => session.project_root === root && (!term || folderMatches || session.title.toLowerCase().includes(term)));
    return { root, name, chats, folderMatches };
  }).filter((group) => !term || group.folderMatches || group.chats.length > 0);

  useEffect(() => {
    function fitSidebar() {
      if (window.innerWidth > 700) setWidth((current) => Math.min(current, maxSidebarWidth()));
    }
    window.addEventListener("resize", fitSidebar);
    return () => window.removeEventListener("resize", fitSidebar);
  }, []);

  function resizeAt(clientX: number) {
    setWidth(Math.max(MIN_SIDEBAR_WIDTH, Math.min(maxSidebarWidth(), Math.round(clientX))));
  }

  function startResize(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    setResizing(true);
    resizeAt(event.clientX);
  }

  function moveResize(event: PointerEvent<HTMLDivElement>) {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) resizeAt(event.clientX);
  }

  function stopResize(event: PointerEvent<HTMLDivElement>) {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    setResizing(false);
  }

  function resizeWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      setWidth((current) => Math.max(MIN_SIDEBAR_WIDTH, Math.min(maxSidebarWidth(), current + (event.key === "ArrowLeft" ? -16 : 16))));
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      setWidth(event.key === "Home" ? MIN_SIDEBAR_WIDTH : maxSidebarWidth());
    }
  }

  function focusSearch() {
    onExpand();
    requestAnimationFrame(() => searchRef.current?.focus());
  }

  return (
    <aside id="sidebar" className={`sidebar${open ? " is-open" : ""}${expanded ? "" : " is-collapsed"}${resizing ? " is-resizing" : ""}`} style={{ "--sidebar-width": `${width}px` } as CSSProperties} aria-label="Projects and chats">
      <div className="window-row">
        <div className="window-brand"><Mascot /><span className="brand-name">oryn<b>.</b></span></div>
        <button className="chrome-button search-button" type="button" aria-label="Find a session" onClick={focusSearch}><Search size={20} /></button>
        <button className="chrome-button sidebar-close" type="button" aria-label="Close sessions" onClick={onClose}><X size={20} /></button>
      </div>

      <div className="sidebar-scroll">
        <nav className="sidebar-nav" aria-label="Workspace navigation">
          <button id="new-session" className="nav-row" type="button" aria-label="New session" title="New session" onClick={onNew}>
            <Bot size={20} strokeWidth={1.7} /><span>New session</span><Plus className="nav-end" size={16} />
          </button>
          <button className="nav-row" type="button" aria-label="Find a session" title="Find a session" onClick={focusSearch}>
            <MessageSquare size={20} strokeWidth={1.7} /><span>Find chats</span><span className="nav-count">{sessions.length}</span>
          </button>
        </nav>

        <section className="session-section" aria-label="Projects and saved chats">
          <div className="section-heading">PROJECTS</div>
          <label className="search-wrap">
            <span className="sr-only">Search projects and chats</span>
            <Search size={16} aria-hidden="true" />
            <input ref={searchRef} id="session-search" type="search" placeholder="Search projects and chats" value={query} onChange={(event) => setQuery(event.target.value)} />
          </label>
          <nav className="project-list" aria-label="Projects and chats">
            {matches.map(({ root, name, chats }, index) => {
              const isExpanded = Boolean(term) || !collapsedProjects.includes(root);
              return <div className="project-group" key={root}>
                <div className={`project-row${project === root ? " is-active" : ""}`}>
                  <button id={index === 0 ? "first-project" : undefined} className="project-select" type="button" title={root} aria-current={project === root ? "location" : undefined} onClick={() => { setCollapsedProjects((current) => current.filter((item) => item !== root)); onProject(root); }}>
                    <Folder size={17} strokeWidth={1.7} aria-hidden="true" /><span>{name}</span>
                  </button>
                  <button className="project-expand" type="button" disabled={Boolean(term)} aria-label={`${isExpanded ? "Collapse" : "Expand"} ${name} chats`} aria-expanded={isExpanded} aria-controls={`project-chats-${index}`} onClick={() => setCollapsedProjects((current) => current.includes(root) ? current.filter((item) => item !== root) : [...current, root])}>
                    <ChevronDown size={15} aria-hidden="true" />
                  </button>
                </div>
                <div id={`project-chats-${index}`} className="session-list" hidden={!isExpanded}>
                  {chats.map((session) => (
                    <div key={session.id} className="session-row">
                      <button className="session-item" type="button" title={session.title} aria-current={selected === session.id ? "page" : undefined} onClick={() => onSelect(session.id)}>
                        <span className="session-copy"><span className="session-title">{session.title}</span><small>{session.message_count} {session.message_count === 1 ? "message" : "messages"}</small></span>
                        {running.includes(session.id) && <span className="session-running" title="Oryn is working"><span className="sr-only">Oryn is working</span></span>}
                      </button>
                      <details className="session-actions">
                        <summary aria-label={`Actions for ${session.title}`} title="Chat actions"><MoreHorizontal size={16} aria-hidden="true" /></summary>
                        <div className="session-menu">
                          <button type="button" onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); onRename(session.id); }}><Pencil size={14} aria-hidden="true" />Rename</button>
                          <button type="button" disabled={running.includes(session.id)} onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); onDelete(session.id); }}><Trash2 size={14} aria-hidden="true" />Delete</button>
                        </div>
                      </details>
                    </div>
                  ))}
                  {chats.length === 0 && !term && <p className="project-empty">No chats yet</p>}
                </div>
              </div>;
            })}
          </nav>
          {term && matches.length === 0 && <p className="sidebar-empty">No projects or chats match your search.</p>}
        </section>
      </div>

      <div className="sidebar-footer">
        <div className="footer-project"><span className="local-dot" aria-hidden="true" /> <span title={project}>{project.split("/").filter(Boolean).pop() || project || "Local project"}</span></div>
        <div className="footer-model"><span>LOCAL WORKSPACE</span><span>{model}</span></div>
      </div>
      <div className="sidebar-resizer" role="separator" aria-label="Resize sidebar" aria-orientation="vertical" aria-controls="sidebar" aria-valuemin={MIN_SIDEBAR_WIDTH} aria-valuemax={maxSidebarWidth()} aria-valuenow={Math.min(width, maxSidebarWidth())} tabIndex={0} onPointerDown={startResize} onPointerMove={moveResize} onPointerUp={stopResize} onPointerCancel={stopResize} onLostPointerCapture={() => setResizing(false)} onKeyDown={resizeWithKeyboard} />
    </aside>
  );
}

type HeaderProps = {
  title: string;
  model: string;
  status: Status;
  sidebarOpen: boolean;
  sidebarExpanded: boolean;
  onMenu: () => void;
  onCollapse: () => void;
  onSettings: () => void;
};

export function Header({ title, model, status, sidebarOpen, sidebarExpanded, onMenu, onCollapse, onSettings }: HeaderProps) {
  return (
    <header className="topbar">
      <div className="topbar-left">
        <button className="chrome-button desktop-sidebar-toggle" type="button" aria-label={sidebarExpanded ? "Collapse sidebar" : "Expand sidebar"} aria-controls="sidebar" aria-expanded={sidebarExpanded} onClick={onCollapse}>{sidebarExpanded ? <PanelLeftClose size={20} /> : <PanelLeftOpen size={20} />}</button>
        <button id="sidebar-toggle" className="chrome-button menu-button" type="button" aria-label={sidebarOpen ? "Close sessions" : "Open sessions"} aria-controls="sidebar" aria-expanded={sidebarOpen} onClick={onMenu}><Menu size={20} /></button>
        <span className="topbar-title">{title}</span><ChevronDown className="title-chevron" size={17} aria-hidden="true" />
      </div>
      <div className="topbar-right">
        <span className="header-model">{model}</span>
        <span id="connection-status" className={`connection-status ${status.kind}`} role="status" aria-live="polite"><span className="local-dot" aria-hidden="true" />{status.message === "Stopped" ? "Stopped" : status.kind === "busy" ? "Working" : status.kind === "error" ? "Error" : "Ready"}</span>
        <button className="chrome-button mcp-settings-button" type="button" aria-label="MCP server settings" title="MCP server settings" onClick={onSettings}><Settings2 size={19} /></button>
      </div>
    </header>
  );
}

type McpSettingsDialogProps = {
  open: boolean;
  servers: McpServerStatus[];
  onClose: () => void;
  onUpdate: (servers: McpServerStatus[]) => void;
  onSave: (enabled: Record<McpServerStatus["name"], boolean>) => Promise<McpServerStatus[]>;
};

export function McpSettingsDialog({ open, servers, onClose, onUpdate, onSave }: McpSettingsDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const dialog = dialogRef.current;
    if (open && dialog && !dialog.open) dialog.showModal();
    else if (!open && dialog?.open) dialog.close();
  }, [open]);

  async function setServerEnabled(name: McpServerStatus["name"], enabled: boolean) {
    setSaving(true);
    setError("");
    const selection = Object.fromEntries(servers.map((server) => [server.name, server.enabled])) as Record<McpServerStatus["name"], boolean>;
    selection[name] = enabled;
    try {
      onUpdate(await onSave(selection));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  }

  return (
    <dialog ref={dialogRef} className="mcp-dialog" aria-labelledby="mcp-dialog-title" aria-describedby="mcp-dialog-description" onClose={onClose} onClick={(event) => { if (event.target === event.currentTarget) dialogRef.current?.close(); }}>
      <div className="mcp-dialog-heading">
        <div><span className="mcp-kicker">TOOLS & CONNECTIONS</span><h2 id="mcp-dialog-title">MCP servers</h2></div>
        <button className="chrome-button" type="button" aria-label="Close MCP settings" onClick={() => dialogRef.current?.close()}><X size={19} /></button>
      </div>
      <p id="mcp-dialog-description" className="mcp-dialog-description">Connect hosted services and choose which tools Oryn can use. Playwright provides the local browser connection.</p>
      <div className="mcp-server-list">
        {servers.map((server) => (
          <section className="mcp-server-row" key={server.name}>
            <div className="mcp-server-copy">
              <div className="mcp-server-title"><h3>{{
                github: "GitHub", context7: "Context7", microsoft_learn: "Microsoft Learn",
                huggingface: "Hugging Face", tavily: "Tavily", firecrawl: "Firecrawl",
                exa: "Exa", linear: "Linear", notion: "Notion", playwright: "Playwright",
              }[server.name]}</h3><span className={`mcp-status ${server.state}`}>{server.state}</span></div>
              <p>{server.access}</p>
              <small>{server.state === "connected" ? `${server.tool_count} tool${server.tool_count === 1 ? "" : "s"} available.` : server.message}</small>
            </div>
            <label className="mcp-switch">
              <span className="sr-only">Enable {server.name}</span>
              <input type="checkbox" checked={server.enabled} disabled={saving} onChange={(event) => void setServerEnabled(server.name, event.target.checked)} />
              <span aria-hidden="true" />
            </label>
          </section>
        ))}
      </div>
      {error && <p className="mcp-settings-error" role="alert">{error}</p>}
      <div className="mcp-dialog-footer"><span role="status" aria-live="polite">{saving ? "Updating server…" : "API keys remain in mcp.env."}</span><button type="button" onClick={() => dialogRef.current?.close()}>Done</button></div>
    </dialog>
  );
}

export function EmptyState() {
  return (
    <section className="empty-state" aria-label="Start a conversation">
      <Mascot size={128} />
      <h1 className="hero-wordmark">ORYN</h1>
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

  const title = item.action === "terminal" ? "Run a terminal command?"
    : item.action === "mcp" ? `Call ${item.target}?`
    : item.action === "edit" ? `Apply proposed edit to ${item.target}?`
    : item.action === "undo" ? `Restore previous contents of ${item.target}?`
    : item.action === "undo_created" ? `Delete ${item.target} to undo Oryn's creation?`
    : `${item.action === "create" ? "Create" : "Replace"} ${item.target}?`;
  const diff = ["edit", "undo", "undo_created"].includes(item.action) ? parseApprovalDiff(item.content) : null;
  const allowLabel = item.action === "edit" ? "Apply edit"
    : item.action === "undo" ? "Restore file"
    : item.action === "undo_created" ? "Delete file"
    : "Allow once";
  return (
    <section className="approval-card" aria-label="Tool approval requested">
      <div className="approval-heading">
        <div className={`approval-label${item.decision === undefined ? "" : item.decision ? " is-approved" : " is-denied"}`}>
          {item.decision === undefined ? "Approval required" : item.decision ? "Approved" : "Denied"}
        </div>
        <h2>{title}</h2>
        <p>{item.decision === undefined ? "Review this action before Oryn continues." : item.decision ? "Approved. Oryn is continuing." : "Denied. Oryn is continuing without this action."}</p>
      </div>
      {diff ? <ApprovalDiff target={item.target} lines={diff.lines} added={diff.added} removed={diff.removed} /> : <pre>{item.content}</pre>}
      {error && <p className="approval-error" role="alert">{error}</p>}
      {item.decision === undefined && <div className="approval-actions">
        <button ref={denyRef} type="button" disabled={submitting} onClick={() => decide(false)}>Deny</button>
        <button className="allow-button" type="button" disabled={submitting} onClick={() => decide(true)}>{allowLabel}</button>
      </div>}
    </section>
  );
}

type ApprovalDiffLine = { kind: "context" | "added" | "removed"; oldNumber: number | null; newNumber: number | null; text: string };

function parseApprovalDiff(source: string): { lines: ApprovalDiffLine[]; added: number; removed: number } | null {
  const lines: ApprovalDiffLine[] = [];
  let oldNumber: number | null = null;
  let newNumber: number | null = null;
  for (const line of source.split(/\r?\n/)) {
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) {
      oldNumber = Number(hunk[1]);
      newNumber = Number(hunk[2]);
      continue;
    }
    if (oldNumber === null || newNumber === null || line === "" || line.startsWith("\\")) continue;
    const marker = line[0];
    if (marker === " ") {
      lines.push({ kind: "context", oldNumber: oldNumber++, newNumber: newNumber++, text: line.slice(1) });
    } else if (marker === "-") {
      lines.push({ kind: "removed", oldNumber: oldNumber++, newNumber: null, text: line.slice(1) });
    } else if (marker === "+") {
      lines.push({ kind: "added", oldNumber: null, newNumber: newNumber++, text: line.slice(1) });
    }
  }
  if (!lines.length) return null;
  return {
    lines,
    added: lines.filter((line) => line.kind === "added").length,
    removed: lines.filter((line) => line.kind === "removed").length,
  };
}

function ApprovalDiff({ target, lines, added, removed }: { target: string; lines: ApprovalDiffLine[]; added: number; removed: number }) {
  return (
    <div className="approval-diff" role="group" aria-label={`Proposed changes to ${target}`}>
      <div className="approval-diff-header">
        <div className="approval-diff-file"><FileCode2 size={15} aria-hidden="true" /><code title={target}>{target}</code></div>
        <div className="approval-diff-counts" role="group" aria-label={`${added} lines added, ${removed} removed`}><span>+{added}</span><span>−{removed}</span></div>
      </div>
      <div className="approval-diff-scroll">
        <div className="approval-diff-lines" role="list" aria-label="Changed lines">
          {lines.map((line, index) => (
            <div className={`approval-diff-line ${line.kind}`} key={`${line.kind}-${line.oldNumber}-${line.newNumber}-${index}`} role="listitem">
              <span className="approval-diff-number" aria-hidden="true">{line.oldNumber ?? ""}</span>
              <span className="approval-diff-number" aria-hidden="true">{line.newNumber ?? ""}</span>
              <span className="approval-diff-marker" aria-hidden="true">{line.kind === "added" ? "+" : line.kind === "removed" ? "−" : ""}</span>
              <span className="sr-only">{line.kind === "added" ? "Added: " : line.kind === "removed" ? "Removed: " : "Context: "}</span>
              <span className="approval-diff-code">{line.text || " "}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function Timeline({ items, status, onDecide }: { items: TimelineItem[]; status: Status; onDecide: (id: string, allow: boolean) => Promise<void> }) {
  const replying = status.kind === "busy" && status.message === "Oryn is replying…";
  const lastItem = items.at(-1);
  const orbState: OrbState = status.message === "Oryn is thinking…" ? "solving" : status.message.toLowerCase().includes("search") ? "searching" : "working";
  return (
    <div id="messages" className="messages" role="log" aria-label="Conversation" aria-live="polite" aria-relevant="additions text">
      {items.map((item) => {
        if (item.kind === "message") return (
          <article key={item.key} className={`message ${item.role}${replying && item === lastItem && item.role === "assistant" ? " is-streaming" : ""}`}>
            {item.role === "assistant" && <div className="message-avatar" aria-hidden="true"><Mascot size={27} /></div>}
            <div className="message-body">
              {item.role === "assistant" && <div className="message-role">Oryn</div>}
              {item.role === "assistant" && item.turnStatus && <div className={`message-turn-status ${item.turnStatus}`} role="status">
                {item.turnStatus === "cancelled" ? "Stopped before finishing" : "Couldn't finish this reply"}
              </div>}
              {item.role === "assistant"
                ? <div className="message-content markdown"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[[rehypeKatex, { throwOnError: false }]]} components={{ a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> }}>{normalizeLatexDelimiters(item.content)}</ReactMarkdown></div>
                : <div className="message-content">{item.content}</div>}
            </div>
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
      {status.kind === "busy" && !replying && status.message !== "Waiting for your decision" &&
        <div className="activity" role="status">
          <ThinkingOrb state={orbState} size={20} theme="light" aria-hidden="true" />
          <span>{status.message}</span>
        </div>}
    </div>
  );
}

type ComposerProps = {
  prompt: string;
  project: string;
  busy: boolean;
  disabled: boolean;
  status: Status;
  onPrompt: (value: string) => void;
  onSend: () => void;
  onCancel: () => void;
  canStop: boolean;
  stopPending: boolean;
  onShortcut: (command: ShortcutId) => void;
};

export function Composer({ prompt, project, busy, disabled, status, onPrompt, onSend, onCancel, canStop, stopPending, onShortcut }: ComposerProps) {
  return (
    <div className="composer-area">
      {status.kind === "error" && <p className="status-message error" role="alert">{status.message}</p>}
      <PromptInputBox value={prompt} onValueChange={onPrompt} onSubmit={onSend} onCancel={onCancel} canStop={canStop} onShortcut={onShortcut} project={project} isLoading={busy} disabled={disabled || stopPending} />
      <div className="composer-footnote">Oryn asks before writing files or running commands.</div>
    </div>
  );
}
