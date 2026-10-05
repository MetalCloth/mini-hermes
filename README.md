# Oryn

Oryn is a local, educational coding assistant with Codex and Gemini model providers. It has a Python agent loop and tools, SQLite chat history, a full-screen terminal UI, MCP connections, and a React dashboard.

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

By default, `./oryn` opens the full-screen Python TUI on its home page with a fresh chat. Open an existing chat through `/sessions`, `Ctrl+O`, or `--resume SESSION_ID`. Type `/` for commands. `/delete` asks for confirmation, removes the current chat, and returns to the home layout; it keeps project files. `Ctrl+P` opens the command palette, `Ctrl+N` starts a chat, and `F2` changes the model. `Enter` sends a message; `Shift+Enter` adds a line. `Ctrl+C` stops a reply or quits when idle.

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

Choose Gemini models from `/models` or launch Oryn with `--model gemini-3.8-flash`. Put `GEMINI_API_KEY=your-key` in `~/.mini-hermes/gemini.env`; the key is read from that file or the `GEMINI_API_KEY` environment variable and sent only in Google's API-key header. Oryn currently lists Gemini 3 text models, supports image input, streamed text, tool calls, and adjustable reasoning. The selected provider is also used by `/computer`.

Use `./oryn repl` for the original line-based terminal chat, or `./oryn dashboard` for the React browser UI.

## Computer mode (selected model)

On this Hyprland desktop, install [CUA Driver](https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.32.0) (`cua-driver` 0.32.0 tested), `grim`, and [dotool](https://github.com/nick-tgcs/dotool`). Put the CUA executable on `PATH` or in this checkout's `.venv/bin/cua-driver`; the latter is installed on this laptop. Oryn starts CUA with `cua-driver serve` and calls it through a private CLI socket, not MCP. Dotool needs `/dev/uinput` write permission. Oryn sets CUA's native Wayland flag for its child process. CUA is the default `/computer` driver; set `ORYN_COMPUTER_DRIVER=dotool` before launching Oryn to use the screenshot + dotool driver for all desktop actions. CUA currently requires one unscaled monitor at `(0, 0)`. Configure the selected provider with `codex login` for Codex or `GEMINI_API_KEY` for Gemini. `/computer` uses the currently selected model, reasoning effort, and speed. The model must support image input. The default model is `gpt-5.6-luna`; choose another with `/models`. `/computer` does not use the OpenRouter key.

Select `/computer` from the command list, or type `/computer` and press Enter. The next message becomes a computer task; `/computer open Settings` starts one directly. The selected model handles that task in Oryn's normal conversation and tool loop, with the same session history, context compaction, reasoning setting, turn budget, and other available tools. There is no forced `computer_plan` call or fixed switch countdown. Esc stops the active turn.

The scoped `computer_observe` tool lists visible CUA windows, observes an exact `pid` and `window_id`, or captures the desktop. A window observation includes a bounded accessibility tree and screenshot where CUA can prove the window image. On Wayland, CUA may return the tree with `surface_identity_unproven`; Oryn then focuses the exact window, refreshes its tree, and captures a desktop image. The model can request tree only for a cheaper later observation. Screenshots go to the selected model as image-bearing tool results for its next decision; saved history retains compact text outcomes, not those screenshots or old control lists.

`computer_act` accepts one action from the latest observation ID, then returns its action outcome and a fresh observation. Old IDs and missing numbered controls are rejected. CUA supplies window lists, accessibility data, screenshots, and native checks; dotool sends all clicks, keys, typing, right clicks, and scrolling. A numbered control click maps its current CUA frame to a dotool coordinate and is refused if that mapping is unsafe. Dotool input to a chosen window first checks an exact Hyprland match and focuses it when needed. Desktop input is rejected if the active window changed since its screenshot. A refused or uncertain action is never replayed automatically. The model is instructed to mark sending, deleting, buying, publishing, and submitting for approval; after approval Oryn reobserves, and the model must reissue the same action from the new observation before input. `computer_ask_user` keeps a clarification in the same turn.

For exact native window or accessibility conditions, `computer_wait` and `computer_act.wait_for` call CUA `verify_state` with a maximum ten-second wait. `unknown` is not success. For visual loading or unsupported conditions, `computer_observe.min_age_ms` waits only until the previous observation is old enough, counting model thinking time, then takes one fresh look. There is no site-specific poller. `/computer --dry-run open Settings` previews the first proposed input without sending it. With `ORYN_COMPUTER_DRIVER=dotool`, observation and input use desktop screenshots and dotool only; exact-window and native verify tools are unavailable. The [measurement plan](src/docs/18-computer-perception-plan.md) and [CUA trial](src/docs/20-cua-driver-hyprland-experiment.md) remain historical records.

For a live computer-mode trace, start Oryn with `./scripts/computer-log.sh run` and run
`./scripts/computer-log.sh follow` in another terminal. The readable view shows model timing,
every tool call used during the task, visible windows and controls, target-to-coordinate mapping,
desktop input results, waits, and the observation returned after each action. IDs link each tool
call, observation, action, and resulting state. Use `./scripts/computer-log.sh follow --raw` for
the underlying JSONL records. Local traces are private files under `logs/`; they can include
typed text and visible UI/accessibility text, but do not save screenshot pixels.

LangSmith export is optional and off by default. Install the project dependencies, then set
`ORYN_LANGSMITH_TRACING=1`, `LANGSMITH_API_KEY`, and optionally `LANGSMITH_PROJECT` before
starting `./scripts/computer-log.sh run`. It sends scrubbed event metadata asynchronously;
screenshots, task/prompt text, final answer text, typed text, full tool results, and full
accessibility-tree excerpts are excluded. UI labels, control frames, and some selection values
remain visible in the remote trace, so enable it only when that screen content is okay to send.
The local JSONL trace remains the detailed source. See [computer trace logging](src/docs/22-computer-tracing.md)
for the event links, privacy boundaries, and limitations.
The old `computer_benchmark.py` applies to historical planner traces, not the current main-loop path.
The [current `/computer` decision record](src/docs/21-cua-main-loop.md) explains why the main loop, CLI/socket transport, scoped observations, conditional waits, and dotool routing were chosen, along with their limits.

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
- `src/providers/` — model clients and the legacy computer planning adapter
- `src/tools/computer_session.py` and `src/tools/computer_driver.py` — scoped computer tools and local input driver
- `src/computer.py` — legacy screenshot/action loop, no longer used by the TUI `/computer` path
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
