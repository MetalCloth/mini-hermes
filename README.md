# Oryn

Oryn is a local, educational coding assistant built around a Codex model. It has a Python agent loop and tools, SQLite chat history, and a React dashboard.

## Run the dashboard

Requirements: Python 3.10+, Node.js/npm, and the Codex CLI.

```fish
python3 -m venv .venv
source .venv/bin/activate.fish
python -m pip install -r requirements.txt
codex login
./oryn dashboard
```

For Bash or Zsh, activate with `source .venv/bin/activate` instead.

The launcher installs the React dependencies on first use, builds the UI, and opens the dashboard at `http://127.0.0.1:9119`. Run it from the project folder you want Oryn to work in. The dashboard is local-only and asks before running terminal commands or writing files.

To use a different project folder, start Oryn from that folder or pass `--project`:

```bash
./oryn dashboard --project /path/to/project
```

## Terminal chat

By default, `./oryn` opens the full-screen Python TUI. Type `/` for commands. `Ctrl+P` opens the command palette, `Ctrl+N` starts a chat, `Ctrl+O` opens saved sessions, and `F2` changes the model. `Enter` sends a message; `Shift+Enter` adds a line. `Ctrl+C` stops a reply or quits when idle.

Click the effort or speed below the prompt, or use `/effort` (`F3`) and `/speed` (`F4`). Each picker reads the selected model's advertised options from `~/.codex/models_cache.json`; unsupported choices are rejected. Choices are saved separately for each model in each session and apply to the next turn. New chats inherit the current choices. Fast mode requests priority processing and can use more credits; availability depends on the account. Unknown models or a missing catalog keep provider defaults until the Codex CLI refreshes its catalog.

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

Codex login uses the local Codex CLI credentials; Oryn does not ask for an OpenAI API key.

## Web tools

`web_search` uses Tavily. `web_extract` and browser interaction use Firecrawl when configured. Copy the relevant key into the local config file shown in [.env.example](.env.example); keep real keys out of the repository:

- `~/.mini-hermes/tavily.env` for Tavily search
- `~/.mini-hermes/firecrawl.env` for page extraction and browser interaction

For a simple page read, Oryn uses `web_extract`. Browser interaction opens a short-lived Firecrawl session for pages that need clicks or form input, then closes it when the agent turn ends. Firecrawl bills browser sessions by duration.

## MCP tools

Oryn connects directly to nine official hosted MCP servers using the installed MCP Python SDK. GitHub and Context7 no longer need Docker or Node.js. Playwright remains a separate local, headless browser connection and needs Node.js 20+.

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

Existing Tavily and Firecrawl keys in `~/.mini-hermes/tavily.env` and `firecrawl.env` are reused if `mcp.env` has no key. Restart Oryn after changing credentials.

Notion's hosted server requires OAuth; a regular Notion integration API key will not work. Sign in explicitly:

```bash
./oryn mcp login notion
./oryn
```

The login command opens your browser and receives the callback on `127.0.0.1:8766`. The MCP SDK handles PKCE and token refresh. OAuth credentials are stored with owner-only permissions in `~/.mini-hermes/mcp-notion-auth.json`. Startup never opens login windows. Missing or expired authorization is reported in MCP status; rerun the login command when needed. Advanced users can supply an existing OAuth access token as `NOTION_ACCESS_TOKEN` in `mcp.env` instead.

GitHub keeps the `repos`, `issues`, and `pull_requests` toolsets in read-only mode. Hugging Face, Linear, Notion, and other tools that are not identified as read-only ask for approval before running. Playwright page-changing actions also ask for approval; arbitrary code execution, screenshots, and saved browser state are not exposed to the model.

The dashboard MCP settings show all connections and retain enable/disable preferences. Existing switches are preserved when the new services are added. In the TUI, `/mcps` lets you search connections, toggle a server with `Enter` or `Ctrl+E`, reconnect with `Ctrl+R`, and start Notion browser sign-in with `Ctrl+L`. `Esc` cancels a pending sign-in. Reconnect reloads keys from `~/.mini-hermes/mcp.env`; changes apply immediately and switches persist across restarts. Connection changes wait until the current answer finishes. Other services use their configured keys. `/tools` shows searchable tool names and short descriptions, with the highlighted tool's full description below. You can also use `./oryn mcp enable github` or `./oryn mcp disable github` from the terminal, then restart Oryn.

If an MCP server is missing or cannot start, Oryn continues with its built-in tools and prints which server was skipped.

## Project structure

- `src/agent/` — prompt/context preparation and the conversation/tool loop
- `src/providers/` — Codex model API client
- `src/tools/` — terminal, file, web, and browser tools
- `src/session/` — SQLite chat history
- `src/web_app.py` — local HTTP API and static React UI server
- `web/src/` — React dashboard

## Development

```bash
python -m unittest discover -s src/tests
cd web && npm run dev
```

The dashboard launcher serves the production build; `npm run dev` is for UI development.
