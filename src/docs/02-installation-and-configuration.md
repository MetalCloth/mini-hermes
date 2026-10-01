# Installation and configuration

[Handbook index](README.md) · [Previous: architecture](architecture.md) · [Next: provider](03-provider-and-models.md)

## 1. Supported first local release

The first supported target is one person running the checkout on their own Linux machine. The executable `oryn` script expects a `.venv` beside the source and works on any selected project folder. This is a local source-checkout release boundary, not a global package, standalone desktop installer, hosted multi-user service, or promise of cross-platform terminal support.

The documented development environment uses Linux and Python 3.14. Dependency metadata allows Python 3.10 for MCP and 3.9 for Textual, but the MCP error-unwrapping code uses Python's `BaseExceptionGroup`, introduced in 3.11. **Use Python 3.11 or newer for this snapshot.** The project's existing short README previously stated 3.10+, which did not account for that runtime path.

| Component | Needed for | Notes |
| --- | --- | --- |
| Python 3.11+ | All interfaces and the harness | Tested local environment: 3.14 |
| `mcp>=2,<3` | MCP integration | Installed SDK version during documentation: 2.2.0 |
| `textual>=8,<9` | Full-screen terminal UI | Rich supplies terminal Markdown rendering |
| Codex CLI with a valid local login | Model authentication and catalog | Oryn reuses CLI credentials |
| Node.js/npm | Dashboard build and local Playwright MCP | Locked Vite requires Node `^20.19.0` or `>=22.12.0`; Playwright setup expects Node.js 20+ |
| `rg` / ripgrep | `search_files` | Searches are delegated to ripgrep |
| `bwrap` / Bubblewrap | Native terminal tool | Linux command isolation; terminal execution refuses to proceed without it |
| Network access | Model, hosted MCPs, Tavily, Firecrawl | Local UI does not imply offline operation |

Dashboard package versions and build commands live in [package.json](../../web/package.json). The Python dependency bounds live in [requirements.txt](../../requirements.txt). No new dependency was introduced to write this handbook.

## 2. First setup

From the repository directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
codex login
./oryn
```

For a fresh checkout, install Python requirements before launch. `./oryn dashboard` also needs
Node/npm; the launcher installs the locked web dependencies with `npm ci` and builds the UI.
Oryn checks the local Codex login shape at startup and shows a generic `codex login` warning
when it is missing or invalid. The check does not refresh credentials or display token values.

The launcher directly invokes `.venv/bin/python`, so activating the environment is optional for these commands. If you prefer activation:

```bash
source .venv/bin/activate
```

Fish uses:

```fish
source .venv/bin/activate.fish
```

No OpenAI API key field is required by the current provider. Its authentication source is the Codex CLI's local login file. Additional web services and MCP connections have their own authorization.

## 3. Launcher behavior

[oryn](../../oryn) resolves its installation directory, finds the local virtual environment, and supplies `PYTHONPATH` so Python can import `src` from that installation. It preserves the caller's working directory, which is the default active project.

```mermaid
flowchart TD
    A["Run oryn from your shell"] --> B["Resolve launcher location"]
    B --> C["Find installation .venv/bin/python"]
    C --> D{"First argument?"}
    D -->|"mcp"| M["Run MCP configuration / OAuth CLI"]
    D -->|"dashboard"| W["Install missing web dependencies, build UI, run web backend"]
    D -->|"repl"| R["Run original line-based chat"]
    D -->|"Anything else"| T["Run Textual TUI with arguments"]
```

For another project, either pass its path or invoke the installed launcher from that folder:

```bash
/path/to/mini-hermes/oryn --project /path/to/my-project
```

The placeholder paths in examples should be replaced with real directories. The repository does not need to live inside the project you want to inspect.

## 4. Interface commands

### Full-screen terminal UI

```bash
./oryn
./oryn --new
./oryn --project /path/to/project
./oryn --list
./oryn --search "a phrase in previous conversations"
./oryn --resume SESSION_ID
./oryn --model gpt-5.6-luna
./oryn --model gemini-3.8-flash
```

Normal TUI launches open the home page with a fresh chat in the selected project. `--new` remains an explicit spelling of that default. To return to saved messages, use `/sessions`, Ctrl+O, or `--resume SESSION_ID`.

The TUI also accepts effort and speed flags. Their values must be supported by the selected model's local catalog entry:

```bash
./oryn --model gpt-6-astra --effort high --speed fast
```

This is an example of setting a model ID, not a promise that a model is enabled for every account. Codex effort and priority choices depend on its local catalog. Gemini reasoning choices depend on the selected Gemini model; Gemini does not use Codex's priority tier.

### Original terminal REPL

```bash
./oryn repl
./oryn repl --project /path/to/project
./oryn repl --new
```

The REPL is useful for studying the harness without a full-screen renderer. It has console approvals and persistent sessions, but does not provide the TUI's picker interface.

### Local dashboard

```bash
./oryn dashboard
./oryn dashboard --project /path/to/project
./oryn dashboard --port 9120 --no-open
```

The default is `127.0.0.1:9119`. The launcher runs `npm ci` when web dependencies are absent, builds the frontend, and starts Python's dashboard server. `--no-open` suppresses automatic browser opening.

For UI development, run the backend and Vite in separate terminals:

```bash
.venv/bin/python -m src.web_app --no-open
```

```bash
cd web
npm ci
npm run dev
```

Vite proxies `/api` to the local backend on port 9119. Its configuration adjusts the origin used for that proxy to match the backend's local-origin checks. The production dashboard launcher serves the built files rather than the Vite development server.

## 5. Local data and credential inventory

`~` means the home directory of the user running Oryn. None of these examples contain a real secret.

| Path | Contents | Owner |
| --- | --- | --- |
| `~/.codex/auth.json` | Codex access/refresh tokens and account ID | Codex CLI; read/refreshed by provider |
| `~/.codex/models_cache.json` | Discovered model catalog and client version metadata | Codex CLI; read by Oryn |
| `~/.mini-hermes/sessions.sqlite3` | Saved transcripts and session metadata | SQLite store |
| `~/.mini-hermes/tavily.env` | Native Tavily search key | Native web tool configuration |
| `~/.mini-hermes/firecrawl.env` | Native Firecrawl extraction/browser key | Native web tool configuration |
| `~/.mini-hermes/gemini.env` | Gemini API key as `GEMINI_API_KEY=...` | Gemini GenerateContent provider |
| `~/.mini-hermes/mcp.env` | Keys for MCP presets | MCP configuration |
| `~/.mini-hermes/mcp-settings.json` | Enabled switches and known-server migration information | MCP preferences |
| `~/.mini-hermes/mcp-notion-auth.json` | Notion OAuth client/token data and metadata | OAuth token storage |

The database and OAuth cache are owner-readable/writable only in the implemented local paths. Credential files should also be restricted to the owner.

## 6. Upgrade and stop

Stop Oryn with Ctrl+C, then update the checkout and reinstall declared dependencies:

```bash
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
npm --prefix web ci
```

The next `./oryn dashboard` build refreshes the bundled frontend. SQLite schema additions are
applied when the store opens. Keep `~/.mini-hermes/` and `~/.codex/` across upgrades; they hold
sessions, settings, credentials, and undo snapshots. Back up those local files before manual
database or credential edits. The app does not automatically upgrade itself.

When Oryn stops, it closes MCP connections, browser sessions, and its in-memory terminal jobs.
Terminal jobs do not resume after restart; completed file changes and their bounded undo records
remain in SQLite.

## 7. Local security boundary

Oryn is single-user software running with the current OS user's permissions. It is designed for
the user to work in a chosen project, not to safely execute hostile projects or share one process
among untrusted accounts. A malicious project file or `AGENTS.md` can try to influence the model;
prompt instructions are not a sandbox. Local skills are instructions only and do not gain code
execution or tool-registration powers.

The dashboard binds to `127.0.0.1`, validates the local Host/Origin, and requires its random
process token for mutations. It has no remote-bind option or public authentication layer. Do not
expose it through a reverse proxy, tunnel, or LAN port. The TUI, REPL, dashboard, and MCP OAuth
callback all use loopback for local web access.

File writes, edits, undo, terminal commands/input, and MCP tools that are not recognized as
read-only require interface approval. Native file tools validate project paths and reject named
private paths; the terminal command is approved and uses Bubblewrap to restrict filesystem writes
to the selected project on Linux. This does not defend against every hostile program, kernel
vulnerability, or secret deliberately copied into the project. Native web/browser actions can
send user-requested data to remote services; do not enter passwords or private secrets in a remote
Firecrawl browser.

Codex credentials are reused by the provider, configured MCP servers run as subprocesses or are
contacted remotely, and model requests send conversation context to the selected provider.
Allowlisted diagnostics omit prompt text, tool arguments, and tool results, but the local SQLite
database itself is not encrypted. Treat the OS account and its configuration directory as the
security boundary.

## 8. Native Tavily and Firecrawl setup

Create the configuration directory, then create the needed files in your editor:

```bash
mkdir -p ~/.mini-hermes
chmod 700 ~/.mini-hermes
```

`~/.mini-hermes/tavily.env`:

```env
TAVILY_API_KEY=
```

`~/.mini-hermes/firecrawl.env`:

```env
FIRECRAWL_API_KEY=
```

After filling the values locally:

```bash
chmod 600 ~/.mini-hermes/tavily.env ~/.mini-hermes/firecrawl.env
```

Run that last command only for files you have actually created. The blank examples are placeholders, not usable credentials.

### Configuration precedence

The helper [local_secret](../security/secrets.py) first checks a nonempty environment variable. If absent, it reads the specified local file. The parser accepts simple `KEY=VALUE` lines, skips blank/comment lines, trims whitespace, and removes matching surrounding quotes.

```mermaid
flowchart TD
    A["Native web_search needs TAVILY_API_KEY"] --> B{"Nonempty environment value?"}
    B -->|"Yes"| E["Use trimmed environment value"]
    B -->|"No"| F["Read ~/.mini-hermes/tavily.env"]
    F --> G{"Key present and nonempty?"}
    G -->|"Yes"| K["Use file value"]
    G -->|"No"| X["Return configuration error; no search request"]
```

**Important:** the native `web_search` tool reads `tavily.env`, not `mcp.env`. The native Firecrawl tools read `firecrawl.env`. MCP presets can fall back to those native key files. The fallback works from MCP configuration toward native files; merely putting a key in `mcp.env` does not configure the native tool. A process environment variable can configure both paths.

Repository `.env.example` documents native placeholders. Oryn does not automatically load every `.env` file from the active project.

For Gemini, copy the `GEMINI_API_KEY` assignment from [.env.example](../../.env.example) into `~/.mini-hermes/gemini.env`, enter the key after `=`, and restrict the file to your user:

```bash
mkdir -p ~/.mini-hermes
chmod 700 ~/.mini-hermes
nano ~/.mini-hermes/gemini.env
chmod 600 ~/.mini-hermes/gemini.env
```

The provider reads the key from `~/.mini-hermes/gemini.env` or the `GEMINI_API_KEY` process environment variable. It does not read a project-local `.env` file. Select a Gemini model with `/models`, `./oryn --model gemini-3.8-flash`, `./oryn repl --model gemini-3.8-flash`, or `./oryn dashboard --model gemini-3.8-flash`.

## 9. MCP configuration

Copy [mcp.env.example](../../mcp.env.example) to the local location, then fill only the services you want:

```bash
mkdir -p ~/.mini-hermes
cp mcp.env.example ~/.mini-hermes/mcp.env
chmod 600 ~/.mini-hermes/mcp.env
```

The keys include:

```env
GITHUB_PERSONAL_ACCESS_TOKEN=
CONTEXT7_API_KEY=
HF_TOKEN=
TAVILY_API_KEY=
FIRECRAWL_API_KEY=
EXA_API_KEY=
LINEAR_API_KEY=
NOTION_ACCESS_TOKEN=
```

Notion normally uses the implemented OAuth login rather than a manually copied token. A standard Notion integration key is not equivalent to the OAuth access token required by its hosted MCP connection.

If a file already contains your keys, edit it rather than copying a template over it. Never commit filled credentials.

### Activate and inspect connections

In the TUI:

1. Open `/mcps`.
2. Search or highlight the service.
3. Use Enter or Ctrl+E to enable/disable.
4. Use Ctrl+R to reconnect after updating credentials.
5. Use Ctrl+L for Notion login.

Reconnect reloads default-preset credentials, so changing the key file does not require closing the TUI. The CLI switches affect the next process startup:

```bash
./oryn mcp enable github
./oryn mcp disable github
./oryn mcp login notion
```

Connections with missing required credentials are marked unavailable. One connection failing does not disable native tools or all other connections.

## 10. Projects and sessions

The project root must be an existing allowed directory. File tools receive project-relative paths such as `src/example.py`. The root is not the same as the current chat title or model ID.

A saved chat is bound to its project. Resuming it restores that root; attempting to rebind the same session to a different root raises an error. This avoids a conversation about project A accidentally writing into project B.

The legacy `main` session is retained for backward compatibility. New sessions use UUID-based IDs. Different current directories and explicit project selection create/use the corresponding session flow rather than requiring a path hardcoded for one person.

## 11. Common setup mistakes

| Mistake | Result | Fix |
| --- | --- | --- |
| Launch without `.venv/bin/python` | Launcher cannot start the application | Create the repository virtual environment and install requirements |
| Missing Codex login | Provider cannot read usable tokens | Run `codex login` with the intended account |
| Key copied only into repository template | Tool still sees no configured key | Put it in the documented home configuration file or environment |
| Tavily key only in `mcp.env` | Tavily MCP may work, native search may fail | Also configure native `tavily.env`, or use environment configuration |
| Integration key supplied to Notion OAuth connection | Authorization fails | Use explicit Notion browser login or an actual compatible OAuth token |
| Playwright enabled without Node/npm | Local MCP startup fails | Install a compatible Node/npm runtime or disable that connection |
| No ripgrep | File search fails | Install `rg` for the supported platform |
| No Bubblewrap | Terminal tool refuses execution | Install supported Bubblewrap or avoid terminal calls |
| Launch dashboard without build via direct module | Static UI may return a build error | Use the launcher or run the frontend build |
| Catalog advertises stale client version | Newer models may reject the request | Upgrade/refresh Codex and its local catalog |

## 12. What setup does not include yet

There is no first-run wizard, account login page for Oryn itself, packaged desktop binary, global `pipx` release workflow, automatic installation of system tools, or general UI for adding arbitrary MCP URLs. The implemented MCP manager controls the known presets. These are product roadmap items, not hidden setup options.
