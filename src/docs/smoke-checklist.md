# Oryn manual smoke checklist

[Handbook index](README.md) · [Testing and troubleshooting](13-testing-troubleshooting-and-operations.md)

This is a repeatable manual plan, not a declaration that every item has already passed. Use a disposable project for file/command checks. Real model and remote service calls may consume account usage. Never put real credentials into test files.

## 1. Preparation

1. Confirm the intended source version and clean/understood working tree.
2. Use the repository virtual environment and a valid Codex login.
3. Create a disposable project containing two small UTF-8 files.
4. Launch `./oryn --project /path/to/disposable-project` for the TUI.
5. Launch `./oryn dashboard --project /path/to/disposable-project` for browser checks.
6. Record interface, terminal/browser size, selected model, enabled connections, and relevant redacted errors.

## 2. Shared harness behavior

| Check | Expected result |
| --- | --- |
| Plain question | Stream finishes with a complete reply; saved chat can be resumed |
| Read two named files | Two calls/results remain correctly paired; answer uses both |
| Search a symbol | Bounded path/line results; private/generated paths excluded |
| Tool argument error | Useful error result reaches model; no false success claim |
| New project | Session remembers selected root |
| Resume session | Restores bound root, avoids silent rebind to another folder |
| Saved search | Finds user/assistant text in expected chats |

## 3. File mutation and undo

1. **Approved creation:** request a disposable file. Review full proposed contents, approve, and compare actual bytes.
2. **Denied replacement:** request replacement, deny, and confirm old contents remain.
3. **Focused edit:** change one unique span. Confirm preview shows only the operation's before/after difference.
4. **Ambiguous edit:** choose repeated old text. Confirm refusal asks for a more specific span.
5. **Stale approval:** change the file externally while its approval waits. Confirm Oryn refuses to overwrite the new state.
6. **Normal undo:** undo a recorded edit. Confirm approval and exact prior bytes/mode restoration.
7. **Undo creation:** undo an Oryn-created file. Confirm explicit delete intent and removal after approval.
8. **Undo with later user edit:** alter the file after Oryn's edit. Confirm undo refuses and preserves your work.
9. **Restart:** confirm chat persists; live undo is unavailable after process restart.
10. **TUI session switching:** inspect app-level undo behavior carefully; the current TUI does not partition its live list for every session.

## 4. Stop and failure recovery

1. Request a response long enough to stream.
2. Type a next-message draft while it is generating.
3. Stop the turn and confirm the draft is retained.
4. Confirm visible partial text is labeled stopped, not complete.
5. Reload/resume the chat and confirm partial content/status persist.
6. Ask “Continue” and verify continuation uses the partial content plus interruption context.
7. Stop at a pending file approval and confirm no file mutation occurs.
8. Trigger a controlled provider failure, confirm partial text is marked failed, then retry after the cause is resolved.
9. Confirm no-text failure displays an honest absence of partial output.

## 5. TUI layout and input

| Check | Expected result |
| --- | --- |
| Enter / Shift+Enter | Send / newline with a terminal that transmits the distinction |
| `/` | Attached suggestions, same width as composer |
| `/sessi` | Sessions suggestion, no unrelated `/new` match |
| Ctrl+P with draft | Command palette; closing preserves draft as implemented |
| F2/F3/F4 | Model, effort, speed choices from catalog |
| Change setting and return to model | Correct saved settings restored |
| `/tools` | Short rows, no unwanted wrapping; selected full description below |
| `/mcps` | Status and management controls remain visible |
| `/help` | Guide scrolls with narrow theme-matched scrollbar |
| Open dialog | Chat remains visible around panel |
| Resize narrow/short | No lost action buttons or excessive right-side gap |
| Hover/drag scrollbar | Muted normal thumb, peach active appearance |

## 6. MCP behavior

1. Connect a keyless documentation server; verify status and tool inventory.
2. Disable it; verify its schemas disappear and preference persists.
3. Enable it; verify tools return without duplicates.
4. Reconnect it; verify old client lifetime ends and new discovery appears.
5. Change a configured key locally and reconnect; verify the preset reloads it.
6. Try a connection with missing credentials; verify only that service is unavailable.
7. Attempt management during an active turn; verify state changes are guarded.
8. For Notion, start explicit login, inspect the visible link, and cancel with Esc.
9. Complete real login only with the intended account; verify saved authorization works after restart.
10. Confirm npm notices and authorization URLs do not print over the TUI.

## 7. Dashboard rendering

| Prompt/content | Expected presentation |
| --- | --- |
| Heading, numbered list, checklist | Structured Markdown |
| Python/JS/JSON/Bash fences | Separate code blocks with language classes |
| Inline and display math | KaTeX layout in dashboard |
| Legacy `\(...\)` / `\[...\]` | Supported prose conversion |
| Same delimiters inside code | Literal preservation |
| Unmatched/escaped formula delimiters | Literal text rather than broad accidental rewrite |
| Long equation/table/code line | Bounded horizontal overflow |
| Interrupted assistant message | Clear stopped/failed label |
| Session switch during another chat's turn | Activity remains attached to original session |
| Rename/delete | Metadata changes; active-session deletion refused |

## 8. Language exploration

Try Hindi, Japanese, Arabic, accented text, emoji, and mixed code/prose. Record actual results separately for dashboard and terminal. Check IME Enter behavior and right-to-left layout. Do not infer natural-language coverage merely from code-fence language tests.

## 9. Automated regression commands

```bash
.venv/bin/python -m unittest discover -s src/tests
```

```bash
cd web
npm run typecheck
npm run test:markdown
npm run build
```

Use the [testing chapter](13-testing-troubleshooting-and-operations.md) to interpret results and distinguish controlled tests from live external-service verification.
