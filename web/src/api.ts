export type SessionSummary = {
  id: string;
  title: string;
  message_count: number;
};

export type SavedMessage = {
  role: "user" | "assistant" | "tool";
  content: string;
  name?: string;
};

export type Bootstrap = {
  token: string;
  project: string;
  model: string;
  sessions: SessionSummary[];
};

export type TurnEvent =
  | { type: "delta"; text: string }
  | { type: "tool_start"; id: string; name: string; detail: string }
  | { type: "tool_result"; id: string; name: string; result: string }
  | { type: "approval"; id: string; action: string; target: string; content: string }
  | { type: "done"; answer: string }
  | { type: "error"; message: string };

export async function getBootstrap(): Promise<Bootstrap> {
  const response = await fetch("/api/bootstrap");
  if (!response.ok) throw new Error(`Could not load the dashboard (${response.status}).`);
  return response.json() as Promise<Bootstrap>;
}

async function requestJson<T>(path: string, token: string, body: object): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mini-Hermes-Token": token },
    body: JSON.stringify(body),
  });
  const result = await response.json() as T & { error?: string };
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status}).`);
  return result;
}

export async function createSession(token: string): Promise<string> {
  const result = await requestJson<{ id: string }>("/api/sessions", token, {});
  return result.id;
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

export async function streamTurn(
  token: string,
  sessionId: string,
  content: string,
  onEvent: (event: TurnEvent) => void,
): Promise<void> {
  const response = await fetch("/api/turns", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mini-Hermes-Token": token },
    body: JSON.stringify({ session_id: sessionId, content }),
  });
  if (!response.ok) {
    const failure = await response.json() as { error?: string };
    throw new Error(failure.error || `Could not start the turn (${response.status}).`);
  }
  if (!response.body) throw new Error("This browser cannot stream responses.");

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
