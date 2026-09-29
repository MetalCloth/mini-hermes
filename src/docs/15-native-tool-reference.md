# Native tool schema reference

[Handbook index](README.md) · [Previous: roadmap](14-roadmap-and-benchmark-readiness.md)

This reference was checked against the actual `tool_schemas()` output in [registry.py](../tools/registry.py). It lists the 22 native model-facing functions. `load_skill` is advertised only when valid local skills exist; MCP names and arguments are discovered dynamically.

## Reading a schema

Every native tool is advertised as a function with `strict: true`. Parameters are an object with `additionalProperties: false`. The required fields below are the schema requirements, even when the corresponding Python function has default arguments. Runtime functions enforce additional limits described in their implementation chapters; a description alone is not a JSON Schema constraint.

Example argument objects are explanatory and do not execute the tools. Browser references must come from a current real snapshot; the invented references here are placeholders. Commands and writes still need their actual approval flow.

## Inventory

| Tool | Required arguments |
| --- | --- |
| [`terminal`](#terminal) | `command` |
| [`terminal_read`](#terminal-read) | `job_id`, `wait_seconds` |
| [`terminal_input`](#terminal-input) | `job_id`, `text` |
| [`terminal_stop`](#terminal-stop) | `job_id` |
| [`read_file`](#read-file) | `path` |
| [`search_files`](#search-files) | `pattern`, `path`, `include`, `exclude`, `literal`, `case_sensitive`, `max_results` |
| [`git_status`](#git-status) | None; use `{}` |
| [`git_diff`](#git-diff) | None; use `{}` |
| [`delegate_read_only`](#delegate-read-only) | `task` |
| [`write_file`](#write-file) | `path`, `content` |
| [`edit_file`](#edit-file) | `path`, `old_text`, `new_text` |
| [`undo_file_change`](#undo-file-change) | None; use `{}` |
| [`web_search`](#web-search) | `query`, `max_results` |
| [`web_extract`](#web-extract) | `url` |
| [`browser_open`](#browser-open) | `url` |
| [`browser_snapshot`](#browser-snapshot) | None; use `{}` |
| [`browser_click`](#browser-click) | `ref` |
| [`browser_fill`](#browser-fill) | `ref`, `text` |
| [`browser_press`](#browser-press) | `key` |
| [`browser_scroll`](#browser-scroll) | `direction`, `pixels` |
| [`browser_wait`](#browser-wait) | `mode`, `value` |
| [`browser_back`](#browser-back) | None; use `{}` |

## terminal

Model-facing name: `terminal`.

### Advertised description

Start a shell command from the project folder when the user asks you to inspect or change it. The user must approve every command. Bubblewrap limits writes to the project folder and hides the user's home; network access remains enabled. Returns a session-local job ID and initial output. Use terminal_read to check output, terminal_input for approved interactive input, and terminal_stop to cancel.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `command` | `string` | Yes | Command to run, e.g. 'git status --short'. |

### Example argument object

```json
{
  "command": "git status --short"
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "command": {
      "type": "string",
      "description": "Command to run, e.g. 'git status --short'."
    }
  },
  "required": [
    "command"
  ],
  "additionalProperties": false
}
```

## terminal-read

Model-facing name: `terminal_read`.

### Advertised description

Read new output from a running terminal job in this chat. Waits at most 10 seconds. Use the exact job ID returned by terminal; output is incremental and session-local.

### Arguments and exact schema

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `job_id` | `string` | Yes | The 12-character job ID returned by `terminal`. |
| `wait_seconds` | `number` | Yes | Wait from 0 to 10 seconds for new output. |

```json
{
  "type": "object",
  "properties": {
    "job_id": {"type": "string", "description": "12-character ID returned by terminal."},
    "wait_seconds": {"type": "number", "description": "How long to wait for new output, from 0 to 10."}
  },
  "required": ["job_id", "wait_seconds"],
  "additionalProperties": false
}
```

## terminal-input

Model-facing name: `terminal_input`.

### Advertised description

Send up to 4,096 UTF-8 bytes to an interactive terminal job. Oryn asks the user to approve the exact input. A newline is added when missing.

### Arguments and exact schema

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `job_id` | `string` | Yes | The 12-character job ID returned by `terminal`. |
| `text` | `string` | Yes | Input to send to the running process. |

```json
{
  "type": "object",
  "properties": {
    "job_id": {"type": "string", "description": "12-character ID returned by terminal."},
    "text": {"type": "string", "description": "Input to send to the running process."}
  },
  "required": ["job_id", "text"],
  "additionalProperties": false
}
```

## terminal-stop

Model-facing name: `terminal_stop`.

### Advertised description

Stop a running terminal job owned by this chat. It sends TERM, then KILL if needed.

### Arguments and exact schema

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `job_id` | `string` | Yes | The 12-character job ID returned by `terminal`. |

```json
{
  "type": "object",
  "properties": {
    "job_id": {"type": "string", "description": "12-character ID returned by terminal."}
  },
  "required": ["job_id"],
  "additionalProperties": false
}
```

## read-file

Model-facing name: `read_file`.

### Advertised description

Read a UTF-8 text file inside the project folder when the user asks about its contents. Give a project-relative path such as 'README.md'. Files outside the project folder are rejected. Returns file text.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `path` | `string` | Yes | Project-relative file path. |

### Example argument object

```json
{
  "path": "README.md"
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": "Project-relative file path."
    }
  },
  "required": [
    "path"
  ],
  "additionalProperties": false
}
```

## search-files

Model-facing name: `search_files`.

### Advertised description

Search text inside the active project folder. Pattern uses ripgrep regex syntax unless literal is true. Restrict to a project-relative file or folder with path, and filter filenames with include/exclude globs. Empty include/exclude means no filter. Hidden and ignored files are normally skipped; private config, generated folders, binary files, and files over 2 MiB are skipped. Returns project-relative path:line:snippet matches; use read_file for more context. Search stops after 10 seconds, 12,000 output characters, or max_results.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `pattern` | `string` | Yes | Text or regex to find, e.g. 'append_messages'. |
| `path` | `string` | Yes | Project-relative file or folder; '.' searches the whole project. |
| `include` | `string` | Yes | Filename glob such as '*.py'; use '' for all files. |
| `exclude` | `string` | Yes | Filename glob such as '*_test.py'; use '' for none. |
| `literal` | `boolean` | Yes | True for exact text; false for regex. |
| `case_sensitive` | `boolean` | Yes | True to match letter case. |
| `max_results` | `integer` | Yes | Maximum matching lines, from 1 to 50; use 30 normally. |

### Example argument object

```json
{
  "pattern": "run_turn",
  "path": "src",
  "include": "*.py",
  "exclude": "test_*.py",
  "literal": true,
  "case_sensitive": true,
  "max_results": 20
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "pattern": {
      "type": "string",
      "description": "Text or regex to find, e.g. 'append_messages'."
    },
    "path": {
      "type": "string",
      "description": "Project-relative file or folder; '.' searches the whole project."
    },
    "include": {
      "type": "string",
      "description": "Filename glob such as '*.py'; use '' for all files."
    },
    "exclude": {
      "type": "string",
      "description": "Filename glob such as '*_test.py'; use '' for none."
    },
    "literal": {
      "type": "boolean",
      "description": "True for exact text; false for regex."
    },
    "case_sensitive": {
      "type": "boolean",
      "description": "True to match letter case."
    },
    "max_results": {
      "type": "integer",
      "description": "Maximum matching lines, from 1 to 50; use 30 normally."
    }
  },
  "required": [
    "pattern",
    "path",
    "include",
    "exclude",
    "literal",
    "case_sensitive",
    "max_results"
  ],
  "additionalProperties": false
}
```

## write-file

Model-facing name: `write_file`.

### Advertised description

Create or replace one UTF-8 text file inside the project folder. Use a project-relative path and provide the complete file content. The user approves every write; existing file content will be replaced. Files outside the project and private config paths (such as .env and key files) are rejected. Parent folders must already exist. Returns a confirmation or an actionable error.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `path` | `string` | Yes | Project-relative destination, e.g. 'notes.txt'. |
| `content` | `string` | Yes | Complete UTF-8 text to write; up to 100,000 characters. |

### Example argument object

```json
{
  "path": "example.txt",
  "content": "A disposable example.\n"
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": "Project-relative destination, e.g. 'notes.txt'."
    },
    "content": {
      "type": "string",
      "description": "Complete UTF-8 text to write; up to 100,000 characters."
    }
  },
  "required": [
    "path",
    "content"
  ],
  "additionalProperties": false
}
```

## edit-file

Model-facing name: `edit_file`.

### Advertised description

Make one focused replacement in an existing UTF-8 project file. Read the file first, then provide exact old_text that occurs once and new_text. Oryn shows the unified diff and must get user approval before applying it. Paths stay inside the project and private config paths are rejected; use write_file to create a file or replace its full contents.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `path` | `string` | Yes | Project-relative path to an existing file. |
| `old_text` | `string` | Yes | Exact, unique text from the current file; include context if needed. |
| `new_text` | `string` | Yes | Replacement text; an empty string deletes the matched text. |

### Example argument object

```json
{
  "path": "example.py",
  "old_text": "return a - b",
  "new_text": "return a + b"
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": "Project-relative path to an existing file."
    },
    "old_text": {
      "type": "string",
      "description": "Exact, unique text from the current file; include context if needed."
    },
    "new_text": {
      "type": "string",
      "description": "Replacement text; an empty string deletes the matched text."
    }
  },
  "required": [
    "path",
    "old_text",
    "new_text"
  ],
  "additionalProperties": false
}
```

## undo-file-change

Model-facing name: `undo_file_change`.

### Advertised description

Undo the most recent approved write_file or edit_file change made by Oryn in this chat session. Restores previous bytes and permissions, or deletes a file Oryn created. The user must approve the undo. It refuses if the file changed afterward. The 20 most recent changes are saved per chat and can be undone after reopening Oryn. Changes to existing files over 1 MB are refused so Oryn can keep a safe snapshot.

### Arguments

No arguments. Send an empty object.

### Example argument object

```json
{}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```

## web-search

Model-facing name: `web_search`.

### Advertised description

Search the public web with Tavily when the user needs current or external information. Returns up to five source titles, URLs, and short snippets. Requires a Tavily API key.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `query` | `string` | Yes | Focused search query. |
| `max_results` | `integer` | Yes | Number of results, from 1 to 5. |

### Example argument object

```json
{
  "query": "Python asyncio documentation",
  "max_results": 3
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": "Focused search query."
    },
    "max_results": {
      "type": "integer",
      "description": "Number of results, from 1 to 5."
    }
  },
  "required": [
    "query",
    "max_results"
  ],
  "additionalProperties": false
}
```

## web-extract

Model-facing name: `web_extract`.

### Advertised description

Read one public web page when search snippets are not enough. Provide its full http:// or https:// URL. Returns the source URL, title, and up to 12,000 characters of page text. Uses Firecrawl when configured, otherwise reads HTML directly. If the page cannot be read, try another result. Use browser_open for pages needing clicks or forms.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `url` | `string` | Yes | Full public page URL, e.g. 'https://example.com/article'. |

### Example argument object

```json
{
  "url": "https://docs.python.org/3/library/asyncio.html"
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": "Full public page URL, e.g. 'https://example.com/article'."
    }
  },
  "required": [
    "url"
  ],
  "additionalProperties": false
}
```

## browser-open

Model-facing name: `browser_open`.

### Advertised description

Open a public web page in a short-lived Firecrawl cloud browser when you need to click, fill a field, or inspect changing page content. Returns the current URL and an accessibility snapshot with element refs such as '@e2'. The browser stays open for this agent turn only and closes after the final answer. Browser sessions use Firecrawl credits, so use web_extract for a simple one-page read. Requires FIRECRAWL_API_KEY.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `url` | `string` | Yes | Full public http:// or https:// URL to open. |

### Example argument object

```json
{
  "url": "https://example.com"
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": "Full public http:// or https:// URL to open."
    }
  },
  "required": [
    "url"
  ],
  "additionalProperties": false
}
```

## browser-snapshot

Model-facing name: `browser_snapshot`.

### Advertised description

Refresh the current Firecrawl browser page's URL and accessibility snapshot. Use after browser_open if the page changes without a click or needs another look. Returns page text and current element refs.

### Arguments

No arguments. Send an empty object.

### Example argument object

```json
{}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```

## browser-click

Model-facing name: `browser_click`.

### Advertised description

Click an element in the current Firecrawl browser using a ref from the latest snapshot, such as '@e2'. Returns the updated URL and page snapshot. Open a page first; only click controls needed for the user's request.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `ref` | `string` | Yes | Element ref from the latest snapshot, e.g. '@e2'. |

### Example argument object

```json
{
  "ref": "@e2"
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "ref": {
      "type": "string",
      "description": "Element ref from the latest snapshot, e.g. '@e2'."
    }
  },
  "required": [
    "ref"
  ],
  "additionalProperties": false
}
```

## browser-fill

Model-facing name: `browser_fill`.

### Advertised description

Clear and type text into an input in the current Firecrawl browser using a ref from the latest snapshot. Returns the updated page snapshot. This does not submit the form; use browser_click on its submit control if needed. The page runs in Firecrawl's cloud browser; do not enter passwords or secrets.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `ref` | `string` | Yes | Input ref from the latest snapshot, e.g. '@e1'. |
| `text` | `string` | Yes | Text to enter, up to 1,000 characters. |

### Example argument object

```json
{
  "ref": "@e3",
  "text": "example"
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "ref": {
      "type": "string",
      "description": "Input ref from the latest snapshot, e.g. '@e1'."
    },
    "text": {
      "type": "string",
      "description": "Text to enter, up to 1,000 characters."
    }
  },
  "required": [
    "ref",
    "text"
  ],
  "additionalProperties": false
}
```

## browser-press

Model-facing name: `browser_press`.

### Advertised description

Press a key on the current Firecrawl page after browser_open. Use Enter to submit a filled form, or Tab/Escape/arrow keys to operate a control. This can trigger page actions. Returns the updated URL and text snapshot.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `key` | `string` | Yes | Key such as Enter, Tab, Escape, ArrowDown, or PageDown. |

### Example argument object

```json
{
  "key": "Enter"
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "key": {
      "type": "string",
      "description": "Key such as Enter, Tab, Escape, ArrowDown, or PageDown."
    }
  },
  "required": [
    "key"
  ],
  "additionalProperties": false
}
```

## browser-scroll

Model-facing name: `browser_scroll`.

### Advertised description

Scroll the current Firecrawl page after browser_open to reveal content below, above, or to a side. Returns the updated URL and text snapshot.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `direction` | `string` | Yes | See implementation |
| `pixels` | `integer` | Yes | Distance from 1 to 2,000 pixels, e.g. 500. |

### Example argument object

```json
{
  "direction": "down",
  "pixels": 500
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "direction": {
      "type": "string",
      "enum": [
        "up",
        "down",
        "left",
        "right"
      ]
    },
    "pixels": {
      "type": "integer",
      "description": "Distance from 1 to 2,000 pixels, e.g. 500."
    }
  },
  "required": [
    "direction",
    "pixels"
  ],
  "additionalProperties": false
}
```

## browser-wait

Model-facing name: `browser_wait`.

### Advertised description

Wait up to 10 seconds for the current Firecrawl page to show a known element, text, URL pattern, or load state after an action. Returns the updated URL and text snapshot. Prefer text or a known element over networkidle on busy pages.

### Arguments

| Field | JSON type | Required | Meaning |
| --- | --- | --- | --- |
| `mode` | `string` | Yes | See implementation |
| `value` | `string` | Yes | For ref: @e2; text: visible words; url: **/results; load: load, domcontentloaded, or networkidle. |

### Example argument object

```json
{
  "mode": "load",
  "value": "domcontentloaded"
}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {
    "mode": {
      "type": "string",
      "enum": [
        "ref",
        "text",
        "url",
        "load"
      ]
    },
    "value": {
      "type": "string",
      "description": "For ref: @e2; text: visible words; url: **/results; load: load, domcontentloaded, or networkidle."
    }
  },
  "required": [
    "mode",
    "value"
  ],
  "additionalProperties": false
}
```

## browser-back

Model-facing name: `browser_back`.

### Advertised description

Go back one page in the current Firecrawl browser history after browser_open. Returns the previous URL and text snapshot.

### Arguments

No arguments. Send an empty object.

### Example argument object

```json
{}
```

### Exact parameter schema

```json
{
  "type": "object",
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```

## git-status

Model-facing name: `git_status`.

Read-only inspection of staged, unstaged, and untracked paths under the active project. It runs
Git with the active project as its working root, restricts pathspecs to that folder, times out
after five seconds, and caps output at 20,000 characters. It does not change the index or
worktree. See the [Git tool details](06-tools-files-terminal-and-undo.md#14-read-only-git-awareness).

```json
{"type":"object","properties":{},"required":[],"additionalProperties":false}
```

## git-diff

Model-facing name: `git_diff`.

Shows staged patch (`index` versus `HEAD`), unstaged patch (`worktree` versus `index`), and
untracked path names separately. Untracked file contents are not included. The same project
scope, five-second timeout, and output cap apply.

```json
{"type":"object","properties":{},"required":[],"additionalProperties":false}
```

## delegate-read-only

Model-facing name: `delegate_read_only`.

For one independent inspection task only. The worker receives an isolated context and can call
`read_file`, `search_files`, `git_status`, and `git_diff`; it has no writes, shell, MCP, or nested
delegation. The task is limited to 2,000 characters. See the [worker limits and evidence format](04-agent-loop-and-context.md#12-one-bounded-read-only-subagent).

```json
{
  "type": "object",
  "properties": {"task": {"type": "string", "description": "One focused inspection task, at most 2,000 characters."}},
  "required": ["task"],
  "additionalProperties": false
}
```

## Related implementation boundaries

- [File tools and terminal](06-tools-files-terminal-and-undo.md) explains validation, concrete mutation, previews, and undo.
- [Web tools](07-web-search-extraction-and-browser.md) explains credentials, URL checks, extraction, browser lifetime, and retries.
- [Approvals](12-approvals-security-and-boundaries.md) records which operations actually require a user decision.
- [MCP discovery](08-mcp-fundamentals-and-connections.md) explains how external schemas are adapted and routed.

To refresh this reference after changing the registry, inspect the current `tool_schemas()` result without starting an MCP client. Do not run example commands or remote calls merely to list definitions.
