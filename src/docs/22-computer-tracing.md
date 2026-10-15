# Computer trace logging

This chapter describes the diagnostic trace written for each TUI turn while Oryn runs with
`ORYN_COMPUTER_LOG_FILE`, normally set by `./scripts/computer-log.sh run`. If the model later
uses the desktop, the same trace includes the full turn from its first model request, including
any web, browser, project, terminal, or MCP tools called before the first desktop action. The
trace is generic: it records event types and IDs, not rules for a particular website or app.

When voice input is used under `computer-log.sh run`, the same JSONL file records recording
start/stop, sample rate, selected input and host API names, audio byte count and duration,
peak/RMS signal levels, capture-overflow status, transcription time, transcript length, and a
safe error code on failure. It never records the audio, transcript text, or API key. Completely
silent recordings are detected locally and are not sent to ElevenLabs.

## What a trace connects

The JSONL file is the detailed record. `./scripts/computer-log.sh follow` renders it as a
timestamped, sequence-numbered terminal view, with color when connected to a terminal.
`NO_COLOR=1` disables color. `./scripts/computer-log.sh follow --raw` shows the raw records.

Each record has a `trace_id` shared by the task and a monotonically increasing `seq`. The
other IDs connect the parts that are easy to lose when reading a flat log:

| ID | Connects |
| --- | --- |
| `call_id` | A model tool call to its completion and bounded result preview |
| `observation_id` | The exact screenshot/accessibility observation used by the model and action |
| `action_id` | The requested action, mapped target, dotool input, outcome, and fresh observation |
| `wait_id` | A native predicate check and the observation captured after it |
| `cause` | A returned observation to the action or wait that led to it |

For a numbered-control click, the local trace records the control index, role, label, frame,
requested screenshot point when applicable, and the mapped monitor point. For other actions,
it records the requested input, the dotool command/result, elapsed time, and whether the next
observation included a screenshot. A focus/edit sequence shares one `action_id`; each attempted
input has a step number and its own outcome, and one observation follows the sequence. Startup events measure the delay from submitting the turn to
the worker, first computer-driver startup, and model/tool-loop activity.
`computer_task` updates appear as ordinary tool-call entries in the local trace. The current task record is stored in SQLite; the trace's bounded argument and result previews are not a replacement for that record.
Each model request records preparation time separately from model-response time; failures and
retries record attempt duration and whether the provider call began. Model records include
counts and timings, not private reasoning text.

```mermaid
flowchart LR
    T["Tool call · call_id"] --> O["Observation · observation_id"]
    O --> A["Action · action_id"]
    A --> I["Dotool input and outcome"]
    I --> N["Fresh observation · cause=action_id"]
    O --> W["Wait · wait_id"]
    W --> R["Fresh observation · cause=wait_id"]
```

The logger keeps bounded details: at most 40 visible windows, 60 actionable controls in a
detailed observation entry, a 1,200-character accessibility excerpt, and 4,000-character
previews for tool arguments and results. It records lengths and whether a preview was cut off.
The live view abbreviates those records; use `--raw` when the full bounded local record is
needed.

## Local privacy boundary

The launcher creates each JSONL file with mode `0600`. It stores no screenshot pixels.
However, local records can contain task text, tool arguments/results, typed text, window
titles, accessibility labels and values, and a short accessibility-tree excerpt. Treat the
file as private; remove it when it is no longer useful. Voice traces also contain microphone
device and host API names, which may identify hardware attached to the local machine.

The logging captures what Oryn can serialize about an observation. It does not preserve the
actual screenshot image, so it cannot later reproduce exactly what the vision model saw.
The trace does record screenshot dimensions, image scope, and whether an image was attached.

## Optional LangSmith export

LangSmith export is explicitly off unless `ORYN_LANGSMITH_TRACING=1` and `LANGSMITH_API_KEY`
are both set. `LANGSMITH_PROJECT` selects the project; the default is `oryn-computer`. With
the tracing launcher enabled, that project can also contain ordinary TUI turns that never use
the desktop.
For example:

```bash
ORYN_LANGSMITH_TRACING=1 \
LANGSMITH_API_KEY='your-key' \
LANGSMITH_PROJECT='oryn-computer' \
./scripts/computer-log.sh run
```

The SDK runs on a background thread so remote tracing does not sit in the action path. Export
is best-effort: a missing SDK, bad key, or network failure is recorded in the local trace;
the local log continues. The background upload may not finish if the process is terminated
immediately after a task.

The remote trace excludes screenshot pixels, task and prompt text, final answer text, typed
text, full tool arguments/results, window titles, and full accessibility-tree excerpts. It
does retain timings, event names, IDs, action/target metadata, and control labels/frames.
Selection values for choice-like controls can also be present because they help diagnose
selection errors. Control labels and values may reveal what is on screen. Only enable remote
tracing when that data is acceptable to send to LangSmith.

The local JSONL file remains the detailed source; LangSmith is a remote overview and is not
needed for local desktop tools to run.

## Limits

- A screenshot is represented by metadata only, not an image artifact.
- Model reasoning is not logged. Tool calls and observable effects are logged, which is enough
  to connect choices to execution without storing hidden reasoning.
- Local tool outputs are bounded previews. Check `result_chars` and `result_truncated` before
  concluding that a result was complete.
- LangSmith delivery is asynchronous and best-effort; a local record can exist even when its
  remote span does not.
- This trace has no app-specific filtering or polling behavior. While enabled, it records each
  TUI turn; desktop turns add the general observation, wait, input, and result events.

## Implementation map

- [`ComputerTrace`](../computer_logging.py) writes local JSONL and queues optional remote
  spans.
- [`ComputerSession`](../tools/computer_session.py) records windows, observations, targets,
  waits, actions, and resulting observations.
- [`HyprlandDriver`](../tools/computer_driver.py) records the actual dotool command and result.
- [`OrynTUI`](../tui_app.py) records all tool calls and model/turn diagnostics for traced TUI
  turns, including those that later use the desktop.
- [`computer_log_view.py`](../../scripts/computer_log_view.py) renders the readable live view.
