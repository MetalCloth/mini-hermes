# Oryn

Oryn is a local, educational coding assistant built around a Codex model. It has a Python agent loop and tools, SQLite chat history, and a React dashboard.

## Run the dashboard

Requirements: Python 3.10+, Node.js/npm, and the Codex CLI.

```fish
python3 -m venv .venv
source .venv/bin/activate.fish
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

```bash
./oryn
./oryn --new
./oryn --list
./oryn --resume SESSION_ID
./oryn --search "text to find"
```

Codex login uses the local Codex CLI credentials; Oryn does not ask for an OpenAI API key.

## Web tools

`web_search` uses Tavily. `web_extract` and browser interaction use Firecrawl when configured. Copy the relevant key into the local config file shown in [.env.example](.env.example); keep real keys out of the repository:

- `~/.mini-hermes/tavily.env` for Tavily search
- `~/.mini-hermes/firecrawl.env` for page extraction and browser interaction

For a simple page read, Oryn uses `web_extract`. Browser interaction opens a short-lived Firecrawl session for pages that need clicks or form input, then closes it when the agent turn ends. Firecrawl bills browser sessions by duration.

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
