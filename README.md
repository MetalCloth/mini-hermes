# Oryn

Oryn is a local, educational coding assistant built around a Codex model. It has a Python agent loop and tools, SQLite chat history, a full-screen terminal UI, MCP connections, and a React dashboard.

## Detailed documentation

Start with the [Oryn engineering handbook](src/docs/README.md). It explains the complete development history, architecture, provider, tool loop, context, sessions, recovery, file editing and undo, web services, MCP/OAuth, both interfaces, testing, and roadmap with diagrams and worked examples.

- [Architecture and request flow](src/docs/architecture.md)
- [Setup and credential configuration](src/docs/02-installation-and-configuration.md)
- [Tool arguments and schemas](src/docs/15-native-tool-reference.md)
- [Troubleshooting](src/docs/13-testing-troubleshooting-and-operations.md)
- [Manual smoke checklist](src/docs/smoke-checklist.md)

## Fresh local install

First supported target: Linux, Python 3.11+, and the Codex CLI. Install `ripgrep` for file search
and `bubblewrap` for the approved terminal tool using your distribution's package manager. The
dashboard needs Node.js `^20.19.0` or `>=22.12.0` and npm; the optional local Playwright MCP also
needs Node.js/npm. The MCP error-handling path uses Python 3.11's `BaseExceptionGroup`.

```fish
git clone https://github.com/MetalCloth/mini-hermes.git
cd mini-hermes
python3 -m venv .venv
source .venv/bin/activate.fish
python -m pip install -r requirements.txt
codex login
./oryn dashboard
```

For Bash or Zsh, activate with `source .venv/bin/activate` instead.

The launcher installs the React dependencies on first use, builds the UI, and opens the dashboard at `http://127.0.0.1:9119`. Run it from the project folder you want Oryn to work in. The dashboard is local-only and asks before running terminal commands or writing files. Without `ripgrep`, `search_files` is unavailable; without Bubblewrap, terminal commands are refused.

The first local release is a single-user Linux checkout, not a globally packaged binary or remote
multi-user server. On startup Oryn checks whether the local Codex login looks configured. A missing
or malformed login produces an actionable `codex login` warning without printing token values.
See the [install, upgrade, and security boundary](src/docs/02-installation-and-configuration.md).

To use a different project folder, start Oryn from that folder or pass `--project`:

```bash
./oryn dashboard --project /path/to/project
```

## Terminal chat

By default, `./oryn` opens the full-screen Python TUI on its home page with a fresh chat. Open an existing chat through `/sessions`, `Ctrl+O`, or `--resume SESSION_ID`. Type `/` for commands. `Ctrl+P` opens the command palette, `Ctrl+N` starts a chat, and `F2` changes the model. `Enter` sends a message; `Shift+Enter` adds a line. `Ctrl+C` stops a reply or quits when idle.

Click the effort or speed below the prompt, or use `/effort` (`F3`) and `/speed` (`F4`). Each picker reads the selected model's advertised options from `~/.codex/models_cache.json`; unsupported choices are rejected. Choices are saved separately for each model in each session and apply to the next turn. New chats inherit the current choices. Fast mode requests priority processing and can use more credits; availability depends on the account. Unknown models or a missing catalog keep provider defaults until the Codex CLI refreshes its catalog.

Paste a PNG or JPEG into the composer with Ctrl+V. The draft shows a pasted-image item with its dimensions; Backspace on an empty draft removes the last one. Up to four images (5 MiB each) are accepted per message. Oryn checks dimensions, removes embedded metadata, and requires the local model catalog to confirm image input. Images remain in that chat's local SQLite history and are included with the next message. Linux image paste reads the desktop clipboard through `wl-paste` on Wayland or `xclip` on X11. Image input uses model capacity and may affect usage.

```bash
./oryn
./oryn --new
./oryn --list
./oryn --resume SESSION_ID
./oryn --search "text to find"
./oryn --model gpt-6-astra --effort high --speed fast
```

You can also type `/effort high`, `/effort default`, `/speed fast`, or `/speed standard` directly. Oryn sends the catalog's client version in provider requests, falling back to the installed `codex --version`, instead of hardcoding the old `0.144.1` version. Open the Codex CLI to refresh its catalog after upgrading it.

Use `./oryn repl` for the original line-based terminal chat, or `./oryn dashboard` for the React browser UI.

## Computer mode (selected Codex model)

On a Hyprland desktop, install `grim` and [wdotool](https://github.com/cushycush/wdotool), and sign in with `codex login`. Oryn also finds `wdotool` in this checkout's `.venv/bin`. Screenshots of the focused monitor are sent at the monitor's native resolution through Oryn's existing Codex provider and login. `/computer` uses the model, reasoning effort, and speed currently selected in Oryn. The model must support image input. The default model is `gpt-5.6-luna`; you can choose another with `/models`. `/computer` does not use the OpenRouter key.

Select `/computer` from the command list, or type `/computer` and press Enter. A slim line above the composer shows that your next message will control the desktop. Type the task normally and press Enter; the line disappears as soon as that message is sent. Press Esc to cancel before sending. You can also enter `/computer open Settings` as a one-message shortcut. You have one second to switch to the target app before the first screenshot. The selected model receives the task and screenshot and returns a checked JSON plan with up to three click, double-click, right-click, type, key, or scroll actions. The local driver runs only these allowlisted actions, then Oryn takes a fresh screenshot and asks the model to verify progress. The model can ask you a clarification; actions it marks as sending, deleting, buying, publishing, or submitting require your approval before execution. After answering or approving in Oryn, switch back to the target app within one second. Oryn takes a fresh screenshot before acting. If the active window changes while Oryn captures the screen or the model plans, it discards that screenshot or plan and reobserves; if it changes during an action batch, completed actions are kept and remaining actions are replanned. An approved action expires if the active window changes or the model proposes a different action. The loop stops when the model confirms completion, after 20 model calls or 5 minutes, after a second desktop-driver failure, or when you stop the active turn.

Before each text-typing action, the local driver waits 150 ms before calling `wdotool type`; this short startup pause is intended to reduce the chance that the first character is dropped.

Use `/computer --dry-run open Settings` to preview the first proposed action batch without sending desktop input.

For a live computer-mode trace, start Oryn with `./scripts/computer-log.sh run` and run
`./scripts/computer-log.sh follow` in another terminal. Traces are saved under `logs/` as
private JSONL files; they include model prompts/plans and text sent to desktop input, but not screenshots.
The live view summarizes plans, actions, and driver errors without repeating the full prompt; use
`./scripts/computer-log.sh follow --raw` to display the raw JSONL records.

Codex login uses the local Codex CLI credentials; Oryn does not ask for an OpenAI API key.

## Turn budgets and recovery

Each user turn allows **40 model rounds, 200 tool calls, and 1,200 seconds** by default.
The TUI, REPL, and dashboard share these launch options:

```bash
./oryn --max-rounds 60 --max-tool-calls 300 --max-turn-seconds 1800
./oryn repl --max-rounds 60
./oryn dashboard --max-turn-seconds 1800
```

Allowed ranges are 1–200 rounds, 1–2,000 tool calls, and 1–7,200 seconds. Exhausting a
budget pauses the turn, retaining completed tool pairs and available partial text. Ask
Oryn to continue to start a fresh bounded turn. Completed actions remain in history;
continuation does not automatically repeat them. Blocking operations keep their own
timeouts, so the time budget is checked at execution boundaries and can be exceeded
while an operation finishes or cleanup runs.

Known temporary model failures before response output and eligible read-only MCP failures
get at most three attempts, with cancellable backoff. Partial model streams and mutations
are not automatically replayed. Retry and pause messages appear in the existing activity
display. See [the loop and recovery details](src/docs/04-agent-loop-and-context.md).

## Context limit and summaries

Oryn estimates the complete model input—including instructions, active tool schemas, recent turns,
and image tiles—and starts compressing older completed turns above **200,000 estimated tokens**.
It aims to return to **180,000 tokens** before sending the next request. Summaries use a separate,
tool-free model call and are saved per chat; the original transcript remains intact. `tiktoken`
provides token estimates when its encoding data is cached; while offline without that data Oryn uses
a conservative UTF-8 byte-count fallback and may summarize earlier. See
[context budgeting and the summary prompt](src/docs/04-agent-loop-and-context.md#8-token-counting-and-the-200k-compaction-trigger).

## Web tools

`web_search` uses Tavily. `web_extract` and browser interaction use Firecrawl when configured. Copy the relevant key into the local config file shown in [.env.example](.env.example); keep real keys out of the repository:

- `~/.mini-hermes/tavily.env` for Tavily search
- `~/.mini-hermes/firecrawl.env` for page extraction and browser interaction

For a simple page read, Oryn uses `web_extract`. Browser interaction opens a short-lived Firecrawl session for pages that need clicks or form input, then closes it when the agent turn ends. Firecrawl bills browser sessions by duration.

## MCP tools

Oryn connects directly to nine official hosted MCP servers using the installed MCP Python SDK. GitHub and Context7 no longer need Docker or Node.js. Playwright remains a separate local, headless browser connection and needs Node.js 20+.

All MCP connections start disabled. Enable only the services you want in `/mcps`; Oryn launches
only explicitly enabled servers. MCP definitions load on demand in the REPL, TUI, and dashboard.
Each model request includes native tools and, when a server is enabled, `load_mcp_tools` with a
compact server directory. The model selects a server; its full schemas become callable in the
next request and remain available for that user turn. `/tools` shows the complete discovered
inventory, including tools not yet loaded into a model request.

| Connection | Official endpoint | Authentication |
| --- | --- | --- |
| GitHub | `https://api.githubcopilot.com/mcp/readonly` | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| Context7 | `https://mcp.context7.com/mcp` | Optional `CONTEXT7_API_KEY` |
| Microsoft Learn | `https://learn.microsoft.com/api/mcp` | None |
| Hugging Face | `https://huggingface.co/mcp` | Optional `HF_TOKEN`; account tools require access |
| Tavily | `https://mcp.tavily.com/mcp/` | `TAVILY_API_KEY` |
| Firecrawl | `https://mcp.firecrawl.dev/v2/mcp` | Optional `FIRECRAWL_API_KEY`; keyless tools are limited |
| Exa | `https://mcp.exa.ai/mcp` | Optional `EXA_API_KEY`; keyless tools are limited |
| Linear | `https://mcp.linear.app/mcp` | `LINEAR_API_KEY` |
| Notion | `https://mcp.notion.com/mcp` | Browser OAuth login |

Fill the blank entries from `mcp.env.example` in `~/.mini-hermes/mcp.env` (never commit credentials). Keys travel in HTTP headers, not URLs:

```env
GITHUB_PERSONAL_ACCESS_TOKEN=
CONTEXT7_API_KEY=
HF_TOKEN=
TAVILY_API_KEY=
FIRECRAWL_API_KEY=
EXA_API_KEY=
LINEAR_API_KEY=
```

Existing Tavily and Firecrawl keys in `~/.mini-hermes/tavily.env` and `firecrawl.env` are reused by MCP presets if `mcp.env` has no key. Native tools read their respective files, not `mcp.env`. After changing MCP credentials, reconnect in the TUI or restart Oryn.

Notion's hosted server requires OAuth; a regular Notion integration API key will not work. Sign in explicitly:

```bash
./oryn mcp login notion
./oryn
```

The login command opens your browser and receives the callback on `127.0.0.1:8766`. The MCP SDK handles PKCE and token refresh. OAuth credentials are stored with owner-only permissions in `~/.mini-hermes/mcp-notion-auth.json`. Startup never opens login windows. Missing or expired authorization is reported in MCP status; rerun the login command when needed. Advanced users can supply an existing OAuth access token as `NOTION_ACCESS_TOKEN` in `mcp.env` instead.

GitHub keeps the `repos`, `issues`, and `pull_requests` toolsets in read-only mode. Hugging Face, Linear, Notion, and other tools that are not identified as read-only ask for approval before running. Playwright page-changing actions also ask for approval; arbitrary code execution, screenshots, and saved browser state are not exposed to the model.

The dashboard MCP settings show all connections and retain enable/disable preferences. Connections are disabled until you opt in; adding a new preset never enables it automatically. In the TUI, `/mcps` lets you search connections, toggle a server with `Enter` or `Ctrl+E`, reconnect with `Ctrl+R`, and start Notion browser sign-in with `Ctrl+L`. `Esc` cancels a pending sign-in. Reconnect reloads keys from `~/.mini-hermes/mcp.env`; changes apply immediately and switches persist across restarts. Connection changes wait until the current answer finishes. Other services use their configured keys. `/tools` shows searchable tool names and short descriptions, with the highlighted tool's full description below. You can also use `./oryn mcp enable github` or `./oryn mcp disable github` from the terminal, then restart Oryn.

If an MCP server is missing or cannot start, Oryn continues with its built-in tools and prints which server was skipped.

## Project structure

- `src/agent/` — prompt/context preparation and the conversation/tool loop
- `src/providers/` — Codex API client and computer planning adapter
- `src/computer.py` and `src/tools/computer_driver.py` — screenshot/action loop and local input driver
- `src/tools/` — terminal, file, web, and browser tools
- `src/session/` — SQLite chat history
- `src/mcp/` — hosted/local connections, discovery, policy, and Notion OAuth
- `src/tui_app.py` and `src/tui.tcss` — full-screen terminal UI
- `src/web_app.py` — local HTTP API and static React UI server
- `web/src/` — React dashboard
- `src/docs/` — engineering handbook and smoke checklist

## Development

```bash
python -m unittest discover -s src/tests
python -m src.evaluation.run
./oryn repl --diagnostics
cd web && npm run dev
```

The dashboard launcher serves the production build; `npm run dev` is for UI development.
The six-case offline evaluation uses a scripted provider and temporary projects; it needs no credentials or network. It covers direct completion, a successful and denied tool cycle, provider failure, interruption with retained evidence, and a current-turn context limit. `--diagnostics` prints recent redacted per-turn metadata from the local session database, including bounded Firecrawl HTTP status/detail/request IDs when supplied safely.

The [shared turn and dashboard API contract](src/docs/17-shared-turn-gateway-and-api.md) documents the common loop, persistence semantics, streaming events, and the lifecycle required before any future scheduler.
