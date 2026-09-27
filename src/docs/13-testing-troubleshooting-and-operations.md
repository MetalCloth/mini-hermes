# Testing, troubleshooting, and operating the current system

[Handbook index](README.md) · [Previous: approval boundaries](12-approvals-security-and-boundaries.md) · [Next: roadmap](14-roadmap-and-benchmark-readiness.md)

Sources: [Python tests](../tests), [Markdown tests](../../web/src/markdown.test.ts), [web scripts](../../web/package.json). End-to-end checks: [Smoke checklist](smoke-checklist.md).

## 1. Different kinds of evidence

| Evidence | What it establishes | What it does not establish |
| --- | --- | --- |
| Source inspection | The implemented mechanism and its conditions | That every runtime condition succeeds |
| Unit/integration test with controlled data | Behavior under defined scenarios | Every external account or service works |
| Headless TUI pilot | Widget interaction, layout, and state transitions | Every terminal/font/key encoding behaves identically |
| Screenshot inspection | Visible appearance at captured sizes | Correct backend authorization or hidden edge cases |
| Live MCP check | One real service flow worked at that time | Permanent availability of all MCPs |
| User testing | A reported real workflow works for that user | Comprehensive regression coverage |
| Agent benchmark | Performance on a controlled task distribution | All safety properties or all product UX requirements |

The user reported successful partial-reply continuation and a working Tavily migration. Those observations complement automated checks; they do not replace them.

## 2. Python regression suite and offline evaluations

The suite is run locally and by `.github/workflows/tests.yml`. Latest verification for this snapshot: **126 tests passed on Python 3.14**. The method count is test evidence, not a quality score.

The source snapshot contains these test methods, counted from its test definitions:

| File | Count | Main coverage |
| --- | ---: | --- |
| [test_chat_demo.py](../tests/test_chat_demo.py) | 9 | REPL history, persistence, safe preview, approval EOF, idle/active Ctrl-C, interrupted tool pairing, search CLI, budget pause/continue, diagnostics |
| [test_codex.py](../tests/test_codex.py) | 16 | Stream parsing/completion, function calls, payloads, image inputs, orphaned calls, failure classification, partial-stream protection, error redaction, Retry-After, cancellation, capabilities, version |
| [test_compression.py](../tests/test_compression.py) | 5 | Summary prompt guardrails, complete-turn compaction, stale checkpoint invalidation, older-image compaction, oversized active-turn refusal |
| [test_context.py](../tests/test_context.py) | 4 | 200k trigger/180k target, whole-turn selection with tool-schema costs, clear overflow, image/non-Latin estimation |
| [test_conversation_loop.py](../tests/test_conversation_loop.py) | 10 | Longer turns, bounded retries, tool/time/round pauses, continuation, cancellation, call pairing, schema selection, redacted diagnostics |
| [test_evaluation.py](../tests/test_evaluation.py) | 1 | Deterministic offline tasks cover a direct answer, file read, and denied write |
| [test_images.py](../tests/test_images.py) | 4 | Clipboard detection, image validation, metadata removal, request formatting, and multimodal context limits |
| [test_mcp.py](../tests/test_mcp.py) | 20 | Hosted/stdio flows, preferences, redaction, schema validation, duplicate names, owner lifetime, reconnect, OAuth, timeout, deferred definitions, read-only transient retries |
| [test_session_store.py](../tests/test_session_store.py) | 9 | Migration, metadata, ordered history, summary checkpoint, durable undo/recovery, redacted diagnostics, transaction behavior, search |
| [test_tools.py](../tests/test_tools.py) | 29 | File boundaries, read/search/write, atomic failure, terminal approval/isolation/jobs/cancellation, Tavily, Firecrawl, browser validation/retry |
| [test_tui_app.py](../tests/test_tui_app.py) | 11 | Budget pause/continue and expired approvals, home-page startup/resume, elapsed reply time, commands during active tools, per-chat undo, effort/speed persistence, MCP controls/login, image paste/send/reopen, compact tools, model/session controls, attached palette layout |
| [test_web_app.py](../tests/test_web_app.py) | 8 | Static/bootstrap/session routes, MCP preferences, streamed turns, approval/denial, stop, partial failure persistence, budget pause/continue and expired approvals |
| **Total** | **126** | Mechanism regression coverage; not a model benchmark |

The count describes test methods, not code coverage percentage or benchmark score. Some methods exercise many scenarios internally.

### Commands

```bash
.venv/bin/python -m unittest discover -s src/tests
./oryn repl --diagnostics
./oryn repl --diagnostics SESSION_ID
./.venv/bin/python -m src.evaluation.run
```

Focused suites:

```bash
.venv/bin/python -m unittest src.tests.test_codex
.venv/bin/python -m unittest src.tests.test_mcp
.venv/bin/python -m unittest src.tests.test_tui_app
.venv/bin/python -m unittest src.tests.test_web_app
```

The `.venv` matters: MCP SDK major-version differences and Textual versions can alter imports or rendering behavior. The evaluation command uses a scripted fake provider, temporary project folders, and real loop/tool dispatch; it makes no network call and needs no credentials. It reports each task's checks, latency, model-request count, tool-call count, and estimated context size. It is a deterministic smoke baseline, not a real-model ability measurement.

### Local diagnostic data

All three launchers write allowlisted per-turn metadata to the private local SQLite database: event category, turn ID, elapsed milliseconds, token estimates, request/tool counts, a fixed native tool label or generic `mcp_tool`, and exception class names. The diagnostic schema rejects arbitrary fields. It does not store prompts, file paths, tool arguments/results, error messages, or provider response bodies. Inspect the most recent records with `./oryn repl --diagnostics`, or pass a saved ID to filter one chat. Events are local metadata, not an encrypted store; the database permission and privacy limits still apply.

The GitHub Actions workflow installs `requirements.txt`, runs the full Python suite, and runs the offline task set. It needs no personal API keys or OAuth credentials. External-service behavior remains covered by mocks in CI and requires separate live checks.

## 3. Frontend checks

From `web`:

```bash
npm run typecheck
npm run test:markdown
npm run build
```

The Markdown suite contains five tests:

1. Convert legacy inline and standalone math delimiters.
2. Preserve inline and fenced code literally.
3. Preserve unmatched/escaped delimiters.
4. Keep bracket math inside prose/tables inline.
5. Render common code-fence language labels.

The build runs TypeScript checking and Vite production compilation. A successful build is not a browser end-to-end test. The current project does not provide a complete automated browser UX suite covering every session/race/accessibility interaction.

## 4. Verification recorded before this documentation task

- The full Python suite passed **86 tests** after MCP-manager/tool-browser changes.
- After the final scrollbar-only update, the **five TUI tests** passed again.
- Live Microsoft Learn MCP checks covered connection, reconnect without duplicate tools, disable removing schemas, and re-enable restoring them.
- Normal and narrow terminal screenshots were visually inspected for picker/control/scrollbar appearance.
- The TUI Notion login workflow was exercised with controlled mocks. A real personal Notion login was not claimed by that test.

These are historical verification results from the implementation work. Writing this handbook did not rerun live account mutations or repeat the whole application suite merely to edit Markdown. Documentation checks are reported separately in the task result.

### Follow-up: on-demand MCP definitions

All **87 Python tests** passed after the loader implementation. The new regression method covers the compact initial catalog, invalid arguments, unknown/disabled servers, empty catalogs, unloaded calls, repeated loading, per-turn isolation, dropped connections, approval denial, credentials excluded from requests, and call/output pairing. Existing oversized-result coverage now loads its MCP definition before calling it.

A live read-only Codex/Microsoft Learn check also passed. The first request contained sixteen native schemas plus the loader (17 total, 11,109 serialized characters). The model called `load_mcp_tools`, received Microsoft Learn's three definitions (20 schemas, 15,140 characters), used `microsoft_docs_search`, and answered with a Microsoft Learn source link. This confirms one real discovery/use flow; it is not a formal task benchmark or an assertion that every server is currently available. Loading adds a model round, so a smaller initial catalog alone does not establish lower total task cost.

The broader suite initially stalled during asyncio executor shutdown inside the restricted execution sandbox. The same checks passed outside it, including local dashboard sockets and MCP subprocess tests.

### Follow-up: TUI undo isolation

The focused undo regression passed with an offline model response through the TUI turn dispatcher. It covers separate histories for chats in the same folder, empty history in new chats/projects, returning to an earlier chat, denied undo, refusal over later user edits, and history cleanup when deleting a chat. At that point in the project the suite had 88 methods; the earlier full-suite run recorded the 87 methods present after MCP loading.

```bash
.venv/bin/python -m unittest -v src.tests.test_tui_app.TUILayoutTests.test_undo_history_stays_with_its_chat_across_switches_and_deletion
```

### Follow-up: commands during active tools

The focused active-command regression passed while a controlled read-file operation was paused in the actual turn worker. It covers slash suggestions and filtering, arrow navigation, Enter opening help/tools/MCP views, Ctrl+P draft restoration, blocked session/model changes retaining their input, prevention of a second turn, and dialog focus when the operation finishes. At that point in the project the suite had 89 methods.

```bash
.venv/bin/python -m unittest -v src.tests.test_tui_app.TUILayoutTests.test_commands_work_during_a_tool_operation_and_preserve_drafts
```

### Follow-up: elapsed reply time

The focused timing regression passed using a controlled monotonic clock and offline provider responses through the actual TUI turn worker. It checks a turn with a tool call, completion/failure/cancellation, reset between turns, seconds/minutes and rounding boundaries, invalid timing values, persistence through a new app/store, transcript refresh in a narrow terminal, and removal of UI timing metadata from model context. At that point in the project the suite had 90 methods.

```bash
.venv/bin/python -m unittest -v src.tests.test_tui_app.TUILayoutTests.test_reply_duration_covers_the_turn_and_survives_reopening
```

### Follow-up: home-page startup

The focused startup regression passed with an existing legacy `main` conversation. It exercises repeated default launches, `--new`, and `--project`, confirms fresh IDs and empty transcripts, preserves the old messages/model, and verifies explicit `--resume`. It also mounts the actual home layout and switches to the saved conversation. At that point in the project the suite had 91 methods.

```bash
.venv/bin/python -m unittest -v src.tests.test_tui_app.TUILayoutTests.test_default_launch_opens_home_and_explicit_resume_keeps_saved_chats
```

### Follow-up: recovery and longer turns

On 27 September 2026, the final full **98-method Python suite passed in 35.944 seconds** after the shared recovery, execution-budget, and display/persistence changes. Local socket/subprocess scenarios ran outside the restricted execution sandbox. The tests use controlled transport/provider responses and disposable projects; no real account mutations or public benchmark runs were required.

New checks cover three-attempt limits, permanent/TLS failures, numeric/date Retry-After handling, no replay after text/function output, no MCP mutation retry, cancellation in backoff, completion beyond eight rounds, partial multi-call pauses, persistence/continuation, retained executed pairs on UI callback failure, and approval expiry in the actual TUI and dashboard paths. Follow-up checks also confirm that result-display failures preserve one copy of the completed response/call in the REPL/dashboard, and that a tool-only paused reply retains its elapsed time on transcript reload.

Normal (120×36) and narrow (65×26) TUI screenshots of a real offline approved-write/budget-pause flow were inspected. They show the retained response, pause label, elapsed time, and usable composer; this checks that flow rather than claiming coverage of every terminal/font/layout combination.

The dashboard type check, Markdown checks, and production build passed. React Doctor reported no findings in the three changed React files; its overall application score was 79/100, so this is not a claim that the whole dashboard is free of issues. The existing production bundle-size warning remains. These checks establish regression evidence for the implemented paths, not token budgeting, durable undo, terminal jobs, or a task benchmark runner.

## 5. Diagnostic method: find the broken boundary

```mermaid
flowchart TD
    A["Observed problem"] --> B{"Where does evidence first fail?"}
    B -->|"Cannot start"| C["Launcher, virtual environment, imports"]
    B -->|"Model request rejected"| D["Auth, version, payload, context pairing"]
    B -->|"Tool fails"| E["Arguments, credentials, permission, service response"]
    B -->|"Text arrives but looks wrong"| F["Markdown/math pipeline or terminal layout"]
    B -->|"Wrong chat gets activity"| G["Session-scoped frontend/worker state"]
    B -->|"Chat lost after restart"| H["Persistence, save errors, session selection"]
    C --> I["Inspect responsible source and relevant test"]
    D --> I
    E --> I
    F --> I
    G --> I
    H --> I
```

Do not begin by changing the system prompt for every failure. If the API rejected an orphaned call, formatting guidance cannot make that request valid.

## 6. Error: missing tool output, HTTP 400

**Symptom:** endpoint says no output was found for a function call.

**Root mechanism:** outbound history contained a function call without its required matching result. Interruptions, failures, or legacy saved state can create that mismatch.

**Current defenses:** the loop records each executed call/result pair before the result callback can fail; the provider filters saved orphaned calls/outputs. Test names include `test_provider_skips_saved_tool_call_without_output` and cancellation/callback pairing tests.

**If it recurs:** inspect the message sequence and IDs using safe local debugging. Check that outputs immediately follow the assistant batch and are serialized only for sent calls. Do not delete all chats as the first repair or blame web search without evidence.

## 7. Error: DuckDuckGo returned no readable results

**Symptom:** old search path returns unavailable/human-verification text.

**Cause:** a scraper could not obtain or parse ordinary readable result HTML.

**Current change:** native search uses Tavily JSON API. Missing key, rejected key, quota, connectivity, invalid response, and valid empty results are different cases.

**If still seen:** verify you are running current code rather than an old process/build. The current native search implementation should not be invoking that old scraper. Check the launcher location and source version.

## 8. Error: newer model requires newer Codex

**Symptom:** HTTP 400 says the chosen model requires an upgrade.

**Cause observed:** Oryn previously advertised an old fixed CLI version in headers.

**Current change:** choose valid version from model cache, installed CLI, or fallback. Catalog capabilities drive settings.

**Diagnosis:** inspect `codex --version`, refresh the installed CLI/catalog, and verify the selected model belongs to the account's current options. Avoid assuming that a hardcoded model name grants access.

## 9. Cannot type while the answer streams

**Cause observed:** a busy flag disabled the composer.

**Current change:** editing and submitting have separate conditions. The input can retain a new draft while the existing turn continues. Stop and brief startup/stop state still have dedicated handling.

**Regression check:** start a long reply, type a draft, stop or wait, and confirm the draft remains. Confirm Enter did not begin a competing same-session turn.

## 10. Equation still looks like raw LaTeX

First identify the interface. The dashboard has React Markdown/math/KaTeX. The TUI uses Rich and does not have identical typeset math support.

For the dashboard, check:

1. Did the model produce recognized delimiters?
2. Did legacy delimiter normalization preserve code while converting prose?
3. Did `remark-math` identify the span?
4. Did KaTeX receive valid LaTeX?
5. Are formula styles and overflow available in the built frontend?
6. Is the browser loading a freshly rebuilt frontend?

Naked `[ ... ]` text with stripped escaping is not automatically the same as recognized display math. No parser can reliably infer every intended formula from arbitrary punctuation.

## 11. TUI startup indentation error

An earlier screenshot showed `IndentationError: unexpected indent` in `src/tui_app.py`. That is a Python source syntax problem before any model request. It was repaired during the TUI iteration; starting the module and the test suite now exercises importability.

If a similar error appears, inspect the indicated file/line and the actual checkout being launched. Rebuilding the React dashboard or changing an MCP key cannot fix Python indentation.

## 12. MCP says not connected

Use `/mcps` to identify the server and its actual status:

| State | Interpretation |
| --- | --- |
| Starting | Connection/discovery still in progress |
| Disabled | User switch prevents connection |
| Unavailable | Enabled, but missing credentials/dependency, timeout, or service failure |
| Connected | Accepted session and discovered tool inventory |

For GitHub/Linear/Tavily, check required local configuration. For Notion, use explicit login. For Playwright, check Node/npm. After filling keys, reconnect to reload them. Check that the startup-ready message handler has refreshed the UI before concluding a live connection failed.

The status does not prove every advertised operation will succeed. Account scope and tool-specific arguments are evaluated at call time.

## 13. MCP cleanup or reconnect errors

Async context resources must be closed by the owning task. The owner-task structure exists to satisfy that lifecycle requirement. Tests verify it, and reconnect removes old schemas/bindings before adding new ones.

If duplicate tools appear, inspect `_reconfigure` removal and adapter name deduplication. If old keys appear to persist, confirm the connection uses default presets and reconnect rebuilt its configuration. If a tool times out, the 90-second call limit is distinct from startup limits.

## 14. npm text overwrites the TUI

The screenshot showing npm notices over the slash palette came from subprocess output bypassing Textual's renderer. Stdio MCP stderr now goes away from the terminal. Notion authorization URLs also use a UI callback instead of unconditional printing inside the TUI.

The rule is simple: the renderer owns the screen. Background workers should post messages, not `print` over it. Console output is appropriate in the separate REPL/CLI context.

## 15. Dirty rows, giant boxes, and unrelated slash suggestions

These were separate presentation mechanisms:

- Oversized cards/composer: excessive sizing/padding and layout anchoring.
- Right-side waste: overly constrained content width and margins.
- Full-screen blackout: opaque modal background.
- Wrapped tool descriptions: full long text in list rows; `no_wrap` insufficient after Textual conversion.
- Wrapped effort descriptions: metadata mixed into option labels.
- `/new` while typing `/session`: description search instead of command-name prefix.
- Bright blue scrollbar: default widget theme not overridden across all scrollables.

Current checks cover attached palette dimensions, narrow pickers, details, and shortcuts. Screenshot inspection complements those tests because an element can be technically present and still look poor.

## 16. File operation refuses a change

Read the error before retrying. Common reasons include an outside/private path, missing parent, non-UTF-8 file, no exact match, multiple matches, mixed line endings, oversized edit, or bytes/mode changed while approval was pending.

The correct response to a stale match is to reread and construct a new focused proposal. The correct response to changed undo state is to leave the file untouched, not reconstruct an inverse operation from memory and overwrite user work.

Durable undo records are scoped by chat and project. A new chat has an empty undo list; switching back or reopening the app restores its latest 20 records. Check the active chat when undo is unavailable. Later changes to a shared project file can still make an earlier undo refuse safely.

## 17. Search/command timeouts and oversized context

Narrow the path, pattern, result count, or task. The current system reports bounded failures rather than automatically splitting a huge task into subagents. At 200,000 estimated request tokens it summarizes older complete turns toward a 180,000-token target. A current turn or pinned instructions that remain too large fail clearly; outputs above the per-tool result cap remain truncated rather than archived.

A turn reaching its round/tool/time allowance is now marked paused, with retained completed pairs and partial text. Ask to continue for a fresh budget, or choose the shared `--max-rounds`, `--max-tool-calls`, and `--max-turn-seconds` launch options within their validated ranges. A pause does not undo work or automatically replay completed actions. A blocking operation may finish after the nominal time allowance; late approvals cannot authorize new actions after it expires.

If history selection rejects the current turn, shorten the current request or reduce attached images. Tool schemas count toward the estimate. When the tokenizer vocabulary has not been cached and Oryn is offline, the byte-count fallback can compact sooner than necessary.

## 18. Safe operational checks

Useful read-only commands:

```bash
git status --short
git log -5 --oneline
.venv/bin/python --version
codex --version
rg --version
bwrap --version
node --version
npm --version
```

Avoid pasting complete auth files, all environment variables, or raw database transcripts into reports. A good report contains interface, selected project/model, relevant error, reproduction steps, expected behavior, actual behavior, and a redacted status/trace.

## 19. What the suite still lacks

There is no complete public benchmark harness, comprehensive multilingual UI suite, full cross-platform terminal matrix, or exhaustive external-account integration suite. The durable undo journal and offline three-task evaluator cover important cases, but race conditions, varied projects, model quality, and terminal/platform combinations need broader evaluation.

Future checks should follow demonstrated risks: context overflow, uncertain side effects, race conditions, large tool catalogs, unsupported terminals, and actual task completion. Increasing test count alone is not a performance strategy.
