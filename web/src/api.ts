export type SessionSummary = {
  id: string;
  title: string;
  message_count: number;
  project_root: string;
};

export type SavedMessage = {
  role: "user" | "assistant" | "tool";
  content: string;
  name?: string;
  turn_status?: "cancelled" | "failed" | "paused";
};

export type McpServerStatus = {
  name: "github" | "context7" | "microsoft_learn" | "huggingface" | "tavily"
    | "firecrawl" | "exa" | "linear" | "notion" | "playwright";
  enabled: boolean;
  state: "connected" | "disabled" | "unavailable" | "starting";
  message: string;
  tool_count: number;
  access: string;
  transport: "http" | "stdio";
};

export type Bootstrap = {
  token: string;
  project: string;
  projects: string[];
  model: string;
  sessions: SessionSummary[];
  mcp_servers: McpServerStatus[];
};

export type TurnEvent =
  | { type: "delta"; text: string }
  | { type: "tool_start"; id: string; name: string; detail: string }
  | { type: "tool_result"; id: string; name: string; result: string }
  | { type: "approval"; id: string; action: string; target: string; content: string }
  | { type: "done"; answer: string }
  | { type: "cancelled" }
  | { type: "paused"; message: string }
  | { type: "progress"; message: string }
  | { type: "error"; message: string };

export class TurnRejected extends Error {}

export async function getBootstrap(): Promise<Bootstrap> {
  const response = await fetch("/api/bootstrap");
  if (!response.ok) throw new Error(`Could not load the dashboard (${response.status}).`);
  return response.json() as Promise<Bootstrap>;
}

async function requestJson<T>(path: string, token: string, body: object, method = "POST"): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Mini-Hermes-Token": token },
    body: JSON.stringify(body),
  });
  const result = await response.json() as T & { error?: string };
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status}).`);
  return result;
}

export async function createSession(token: string, projectRoot: string): Promise<string> {
  const result = await requestJson<{ id: string }>("/api/sessions", token, { project_root: projectRoot });
  return result.id;
}

export async function renameSession(token: string, id: string, title: string): Promise<void> {
  await requestJson(`/api/sessions/${encodeURIComponent(id)}`, token, { title }, "PATCH");
}

export async function deleteSession(token: string, id: string): Promise<void> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: { "X-Mini-Hermes-Token": token },
  });
  const result = await response.json() as { error?: string };
  if (!response.ok) throw new Error(result.error || `Could not delete chat (${response.status}).`);
}

export async function cancelTurn(token: string, id: string): Promise<void> {
  await requestJson("/api/turns/cancel", token, { session_id: id });
}

export async function getSession(id: string): Promise<SavedMessage[]> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(id)}`);
  const result = await response.json() as { messages?: SavedMessage[]; error?: string };
  if (!response.ok) throw new Error(result.error || `Could not load session (${response.status}).`);
  return result.messages || [];
}

export async function decideApproval(token: string, id: string, allow: boolean): Promise<void> {
  await requestJson("/api/approvals", token, { id, allow });
}

export async function updateMcpServers(
  token: string,
  enabled: Record<McpServerStatus["name"], boolean>,
): Promise<McpServerStatus[]> {
  const result = await requestJson<{ mcp_servers: McpServerStatus[] }>("/api/mcp", token, { enabled });
  return result.mcp_servers;
}

export async function streamTurn(
  token: string,
  sessionId: string,
  content: string,
  onEvent: (event: TurnEvent) => void,
  onStarted?: () => void,
): Promise<void> {
  const response = await fetch("/api/turns", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mini-Hermes-Token": token },
    body: JSON.stringify({ session_id: sessionId, content }),
  });
  if (!response.ok) {
    const failure = await response.json() as { error?: string };
    throw new TurnRejected(failure.error || `Could not start the turn (${response.status}).`);
  }
  if (!response.body) throw new Error("This browser cannot stream responses.");
  onStarted?.();

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let newline = buffer.indexOf("\n");
    while (newline !== -1) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      if (line) onEvent(JSON.parse(line) as TurnEvent);
      newline = buffer.indexOf("\n");
    }
    if (done) break;
  }
}
