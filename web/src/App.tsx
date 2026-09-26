import { useEffect, useRef, useState } from "react";
import { cancelTurn, createSession, decideApproval, deleteSession, getBootstrap, getSession, renameSession, streamTurn, TurnRejected, updateMcpServers, type McpServerStatus, type SavedMessage, type SessionSummary, type TurnEvent } from "./api";
import { Composer, EmptyState, Header, McpSettingsDialog, Sidebar, Timeline, type Status, type TimelineItem } from "./components";
import type { ShortcutId } from "@/components/ui/ai-prompt-box";

function savedItems(messages: SavedMessage[]): TimelineItem[] {
  return messages.flatMap<TimelineItem>((message, index) => {
    if (message.role === "tool") return [{ key: `saved-${index}`, kind: "tool" as const, name: message.name || "Tool", detail: "", result: message.content }];
    if ((message.role === "user" || message.role === "assistant") && message.content) {
      return [{ key: `saved-${index}`, kind: "message" as const, role: message.role, content: message.content, turnStatus: message.turn_status }];
    }
    return [];
  });
}

const ready: Status = { kind: "ready", message: "" };

export default function App() {
  const [token, setToken] = useState("");
  const [project, setProject] = useState("");
  const [projects, setProjects] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [mcpServers, setMcpServers] = useState<McpServerStatus[]>([]);
  const [mcpSettingsOpen, setMcpSettingsOpen] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [chats, setChats] = useState<Record<string, TimelineItem[]>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [running, setRunning] = useState<string[]>([]);
  const [stoppable, setStoppable] = useState<string[]>([]);
  const [starting, setStarting] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarExpanded, setSidebarExpanded] = useState(true);
  const [statuses, setStatuses] = useState<Record<string, Status>>({});
  const [appStatus, setAppStatus] = useState<Status>(ready);
  const loadVersion = useRef(0);
  const creating = useRef(false);
  const active = useRef(new Set<string>());
  const stopping = useRef(new Set<string>());
  const scrollRef = useRef<HTMLDivElement>(null);
  const draftKey = selected ?? `project:${project}`;
  const prompt = drafts[draftKey] ?? "";
  const items = selected ? chats[selected] ?? [] : [];
  const busy = selected ? running.includes(selected) : starting;
  const status = appStatus.kind === "error" ? appStatus : selected ? statuses[selected] ?? ready : appStatus;
  const canStop = Boolean(selected && stoppable.includes(selected));
  const stopPending = canStop && status.kind === "busy" && status.message === "Stopping Oryn…";

  function updateItems(id: string, change: (current: TimelineItem[]) => TimelineItem[]) {
    setChats((current) => ({ ...current, [id]: change(current[id] ?? []) }));
  }

  function setPrompt(value: string) {
    setDrafts((current) => ({ ...current, [draftKey]: value }));
  }

  function setRunningSession(id: string, isRunning: boolean) {
    if (isRunning) active.current.add(id);
    else active.current.delete(id);
    setRunning([...active.current]);
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const version = ++loadVersion.current;
        const data = await getBootstrap();
        if (cancelled) return;
        setToken(data.token);
        setProject(data.project);
        setProjects(data.projects);
        setModel(data.model);
        setMcpServers(data.mcp_servers ?? []);
        setSessions(data.sessions);
        if (data.sessions[0]) {
          const history = await getSession(data.sessions[0].id);
          if (cancelled || version !== loadVersion.current) return;
          setSelected(data.sessions[0].id);
          setProject(data.sessions[0].project_root);
          setChats((current) => ({ ...current, [data.sessions[0].id]: savedItems(history) }));
        }
      } catch (cause) {
        if (!cancelled) setAppStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [items, selected]);

  useEffect(() => {
    const title = sessions.find((session) => session.id === selected)?.title || "New session";
    document.title = `${title} · Oryn`;
  }, [sessions, selected]);

  useEffect(() => {
    function escape(event: KeyboardEvent) { if (event.key === "Escape") setSidebarOpen(false); }
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, []);

  async function refreshSessions() {
    const data = await getBootstrap();
    setSessions(data.sessions);
    setProjects(data.projects);
  }

  async function openSession(id: string) {
    const version = ++loadVersion.current;
    try {
      if (!(id in chats)) {
        const history = await getSession(id);
        setChats((current) => id in current ? current : { ...current, [id]: savedItems(history) });
      }
      if (version !== loadVersion.current) return;
      setSelected(id);
      setProject(sessions.find((session) => session.id === id)?.project_root || project);
      setSidebarOpen(false);
      setAppStatus(ready);
    } catch (cause) {
      setAppStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
    }
  }

  async function newSession(): Promise<string | null> {
    if (!token || creating.current) return null;
    creating.current = true;
    const version = ++loadVersion.current;
    const root = project;
    try {
      const id = await createSession(token, root);
      if (version !== loadVersion.current) {
        await refreshSessions();
        return null;
      }
      setSelected(id);
      setChats((current) => ({ ...current, [id]: [] }));
      setSessions((current) => [{ id, title: "New session", message_count: 0, project_root: root }, ...current]);
      setAppStatus(ready);
      setSidebarOpen(false);
      requestAnimationFrame(() => document.getElementById("prompt")?.focus());
      return id;
    } catch (cause) {
      setAppStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
      return null;
    } finally {
      creating.current = false;
    }
  }

  function selectProject(root: string) {
    ++loadVersion.current;
    setProject(root);
    setSelected(null);
    setAppStatus(ready);
    setSidebarOpen(false);
    requestAnimationFrame(() => document.getElementById("prompt")?.focus());
  }

  function runShortcut(command: ShortcutId) {
    setPrompt("");
    if (command === "new") void newSession();
    if (command === "find" || command === "projects") {
      setSidebarExpanded(true);
      setSidebarOpen(true);
      requestAnimationFrame(() => document.getElementById(command === "find" ? "session-search" : "first-project")?.focus());
    }
    if (command === "sidebar") {
      if (window.innerWidth <= 700) setSidebarOpen((open) => !open);
      else setSidebarExpanded((expanded) => !expanded);
    }
  }

  async function approve(sessionId: string, id: string, allow: boolean) {
    await decideApproval(token, id, allow);
    updateItems(sessionId, (current) => current.map((item) => item.kind === "approval" && item.id === id ? { ...item, decision: allow } : item));
    setStatuses((current) => ({ ...current, [sessionId]: current[sessionId]?.message === "Waiting for your decision"
      ? { kind: "busy", message: "Oryn is continuing…" } : current[sessionId] ?? ready }));
  }

  async function stopTurn(id: string) {
    stopping.current.add(id);
    setStatuses((current) => ({ ...current, [id]: { kind: "busy", message: "Stopping Oryn…" } }));
    try {
      await cancelTurn(token, id);
    } catch (cause) {
      stopping.current.delete(id);
      setStatuses((current) => ({ ...current, [id]: { kind: "error", message: cause instanceof Error ? cause.message : String(cause) } }));
    }
  }

  async function renameChat(id: string) {
    const session = sessions.find((item) => item.id === id);
    if (!session) return;
    const title = window.prompt("Rename chat", session.title);
    if (title === null) return;
    try {
      await renameSession(token, id, title);
      setSessions((current) => current.map((item) => item.id === id ? { ...item, title: title.trim().replace(/\s+/g, " ") } : item));
    } catch (cause) {
      setAppStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
    }
  }

  async function removeChat(id: string) {
    const session = sessions.find((item) => item.id === id);
    if (!session || !window.confirm(`Delete “${session.title}” and its saved messages?`)) return;
    try {
      await deleteSession(token, id);
      setSessions((current) => current.filter((item) => item.id !== id));
      setChats((current) => {
        const remaining = { ...current };
        delete remaining[id];
        return remaining;
      });
      if (selected === id) {
        setSelected(null);
        setProject(session.project_root);
        setAppStatus(ready);
      }
    } catch (cause) {
      setAppStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) });
    }
  }

  async function send() {
    const text = prompt.trim();
    if (!text || !token || creating.current || (selected && active.current.has(selected))) return;
    let sessionId = selected;
    const userKey = crypto.randomUUID();
    const streamedAssistantKeys = new Set<string>();
    function markPartialReply(id: string, turnStatus: "cancelled" | "failed") {
      updateItems(id, (current) => current.map((item) =>
        item.kind === "message" && streamedAssistantKeys.has(item.key)
          ? { ...item, turnStatus }
          : item,
      ));
    }
    const root = project;
    const version = loadVersion.current;
    if (!sessionId) {
      creating.current = true;
      setStarting(true);
    }
    setAppStatus(ready);
    setPrompt("");
    try {
      if (!sessionId) {
        const id = await createSession(token, root);
        sessionId = id;
        setSessions((current) => [{ id, title: "New session", message_count: 0, project_root: root }, ...current]);
        creating.current = false;
        setStarting(false);
        if (version === loadVersion.current) {
          setSelected(id);
          setDrafts((current) => ({ ...current, [id]: current[draftKey] ?? "", [draftKey]: "" }));
        }
      }
      const turnId = sessionId;
      setRunningSession(turnId, true);
      setStatuses((current) => ({ ...current, [turnId]: { kind: "busy", message: "Oryn is thinking…" } }));
      updateItems(turnId, (current) => [...current, { key: userKey, kind: "message", role: "user", content: text }]);
      let currentTextKey: string | null = null;
      let finished = false;
      const toolKeys = new Map<string, string>();

      function onEvent(event: TurnEvent) {
        if (event.type === "delta") {
          setStatuses((current) => stopping.current.has(turnId) ? current : ({ ...current, [turnId]: { kind: "busy", message: "Oryn is replying…" } }));
          if (!currentTextKey) {
            const key = crypto.randomUUID();
            currentTextKey = key;
            streamedAssistantKeys.add(key);
            updateItems(turnId, (current) => [...current, { key, kind: "message", role: "assistant", content: event.text }]);
          } else {
            const key = currentTextKey;
            updateItems(turnId, (current) => current.map((item) => item.key === key && item.kind === "message" ? { ...item, content: item.content + event.text } : item));
          }
        } else if (event.type === "tool_start") {
          currentTextKey = null;
          const key = crypto.randomUUID();
          toolKeys.set(event.id, key);
          updateItems(turnId, (current) => [...current, { key, kind: "tool", name: event.name, detail: event.detail, result: "" }]);
          setStatuses((current) => stopping.current.has(turnId) ? current : ({ ...current, [turnId]: { kind: "busy", message: `Using ${event.name.replaceAll("_", " ")}…` } }));
        } else if (event.type === "tool_result") {
          const key = toolKeys.get(event.id);
          updateItems(turnId, (current) => current.map((item) => item.key === key && item.kind === "tool" ? { ...item, result: event.result || "No output" } : item));
          setStatuses((current) => stopping.current.has(turnId) ? current : ({ ...current, [turnId]: { kind: "busy", message: "Oryn is working…" } }));
        } else if (event.type === "approval") {
          updateItems(turnId, (current) => [...current, { key: crypto.randomUUID(), kind: "approval", id: event.id, action: event.action, target: event.target, content: event.content }]);
          setStatuses((current) => stopping.current.has(turnId) ? current : ({ ...current, [turnId]: { kind: "busy", message: "Waiting for your decision" } }));
        } else if (event.type === "done") {
          if (!currentTextKey && event.answer) {
            updateItems(turnId, (current) => [...current, { key: crypto.randomUUID(), kind: "message", role: "assistant", content: event.answer }]);
          }
          finished = true;
          stopping.current.delete(turnId);
          setStatuses((current) => ({ ...current, [turnId]: ready }));
        } else if (event.type === "cancelled") {
          markPartialReply(turnId, "cancelled");
          finished = true;
          stopping.current.delete(turnId);
          setStatuses((current) => ({ ...current, [turnId]: { kind: "ready", message: "Stopped" } }));
        } else if (event.type === "error") {
          markPartialReply(turnId, "failed");
          finished = true;
          stopping.current.delete(turnId);
          setStatuses((current) => ({ ...current, [turnId]: { kind: "error", message: event.message } }));
        }
      }

      await streamTurn(token, sessionId, text, onEvent, () => {
        setStoppable((current) => current.includes(turnId) ? current : [...current, turnId]);
      });
      if (!finished) throw new Error("The connection ended early. Reopen the session to inspect what was saved.");
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      const failedId = sessionId;
      if (failedId) {
        markPartialReply(failedId, stopping.current.has(failedId) ? "cancelled" : "failed");
        if (cause instanceof TurnRejected) {
          updateItems(failedId, (current) => current.filter((item) => item.key !== userKey));
          setDrafts((current) => ({ ...current, [failedId]: current[failedId] ? `${text}\n${current[failedId]}` : text }));
        }
        setStatuses((current) => ({ ...current, [failedId]: { kind: "error", message } }));
      }
      else {
        setDrafts((current) => ({ ...current, [draftKey]: current[draftKey] ? `${text}\n${current[draftKey]}` : text }));
        setAppStatus({ kind: "error", message });
      }
    } finally {
      if (sessionId) {
        stopping.current.delete(sessionId);
        setRunningSession(sessionId, false);
        setStoppable((current) => current.filter((id) => id !== sessionId));
      }
      else {
        creating.current = false;
        setStarting(false);
      }
      try { await refreshSessions(); }
      catch (cause) { setAppStatus({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) }); }
    }
  }

  const title = sessions.find((session) => session.id === selected)?.title || "New session";
  return (
    <div className="app-shell">
      <Sidebar project={project} projects={projects} model={model} sessions={sessions} selected={selected} running={running} open={sidebarOpen} expanded={sidebarExpanded} onClose={() => setSidebarOpen(false)} onExpand={() => setSidebarExpanded(true)} onNew={() => void newSession()} onSelect={(id) => void openSession(id)} onRename={(id) => void renameChat(id)} onDelete={(id) => void removeChat(id)} onProject={selectProject} />
      <main className="workspace">
        <Header title={title} model={model} status={status} sidebarOpen={sidebarOpen} sidebarExpanded={sidebarExpanded} onMenu={() => setSidebarOpen((open) => !open)} onCollapse={() => setSidebarExpanded((expanded) => !expanded)} onSettings={() => setMcpSettingsOpen(true)} />
        <div id="chat-scroll" className="chat-scroll" ref={scrollRef}>
          {items.length === 0 ? <EmptyState /> : <Timeline items={items} status={status} onDecide={(id, allow) => approve(selected!, id, allow)} />}
        </div>
        <Composer prompt={prompt} project={project} busy={busy} disabled={!token} status={status} canStop={canStop} stopPending={stopPending} onPrompt={setPrompt} onSend={() => void send()} onCancel={() => selected && void stopTurn(selected)} onShortcut={runShortcut} />
      </main>
      <McpSettingsDialog open={mcpSettingsOpen} servers={mcpServers} onClose={() => setMcpSettingsOpen(false)} onUpdate={setMcpServers} onSave={(enabled) => updateMcpServers(token, enabled)} />
    </div>
  );
}
