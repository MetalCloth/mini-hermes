# Computer-use cheat sheet

This is a quick reference for Oryn's current computer tools in regular TUI chat. The model calls
`computer_*` tools; by default, Oryn uses CUA to read the local desktop and dotool to send input.
Native `browser_*` tools control a separate remote Firecrawl browser, not local Brave.
With `ORYN_COMPUTER_DRIVER=dotool`, Hyprland workspace listing and focus still work,
but observations use desktop screenshots; exact CUA windows, accessibility controls,
and native checks are unavailable.

## The short version

| Part | Job |
| --- | --- |
| `computer_task` | Optional saved checklist for longer tasks: goal, constraints, steps, done checks, progress, and evidence. It does not control the screen or prove success. |
| `computer_observe` | Look at the desktop, a list of windows, or one window. It does not send input. |
| CUA | Read windows, screenshots, accessibility data, and exact native conditions. |
| `computer_act` + dotool | Send a click, key, text, or scroll, then return a fresh observation. |
| `computer_wait` | Wait for one supported CUA condition in an exact window, then return a fresh observation. |
| `computer_ask_user` | Ask for a clarification during the same turn; refresh the current observation with the answer when possible. |

## Ways to observe

| Model call | Desktop read | What the model receives |
| --- | --- | --- |
| `computer_observe({"mode":"windows"})` | Hyprland `clients -j`, plus CUA `list_windows` when available | Windows across workspaces, sorted by workspace, with title, app, `pid`, workspace, `is_active`, and a one-use `window_index` when focusable. `true` confirms foreground, `false` means that entry is not foreground, and `null` means focus could not be read. Uniquely matched CUA windows also have `window_id`. No screenshot or accessibility tree. |
| `computer_observe({"mode":"active"})` | Match the focused Hyprland window with `list_windows`, then `get_window_state` | That window's screenshot and bounded accessibility controls/tree when a unique match exists. Otherwise a desktop screenshot. |
| `computer_observe({"mode":"window","pid":4321,"window_id":71})` | `get_window_state` | The specified window's bounded controls/tree and a screenshot when available. On Wayland, Oryn may fall back to a desktop screenshot while keeping the exact window's tree. |
| `computer_observe({"mode":"desktop"})` | `get_desktop_state` | Full-screen screenshot and the active window's app name/title when available, plus read-only labels when CUA can safely match it. These labels have no actionable indices; use `mode="active"` for `click_element`. |
| `computer_observe({"mode":"desktop","observation_id":"obs-a","region":{"x":400,"y":150,"width":300,"height":250}})` | One native-resolution desktop capture, cropped to a region of the latest full desktop image | One zoomed image with a new ID. The region uses `obs-a` pixels; a click from the crop uses crop-local pixels and maps back to the monitor. A crop click returns a fresh full desktop image. |

`start_session` and `end_session` prepare and close CUA; they are not screen reads.
The accessibility tree is what an app exposes to the desktop, not a direct browser DOM feed.
Some apps expose only a generic window. Window and active observations send at most 60 actionable
controls and a 1,200-character tree excerpt to the model. When a document is exposed, its
controls come before browser chrome in that bounded list.
`mode="windows"` has no image but supplies an `observation_id` for `focus_window`;
`mode="desktop"` always has an image. `image=false` can skip an
exact-window screenshot, but an active-mode fallback can still return a desktop image.
The full desktop overview uses the monitor's native pixel dimensions. Zooming is optional for
small or uncertain visual targets; the crop also keeps its native pixel dimensions.
Each result says `image_scope` so the model can distinguish a full desktop image from a crop.

### Open an app without claiming focus too early

For a request to open or bring a named app forward, check `mode="windows"` first. If the
matching entry has `is_active:true`, it is already in front. If it exists with `is_active:false`,
focus that entry using its current `window_index`; the action confirms the selected window became
active and returns a fresh observation. If no matching window exists in a complete list, use the
desktop launcher. If `truncated:true` and the app is absent, do not assume it is closed or launch a
possible duplicate; ask how to proceed.
A listed app with `is_active:false` proves it exists, not that it is in front. If `is_active` is
`null`, focus is unknown; use a fresh desktop/active observation or focus action before claiming
success. After launching, inspect the action's screenshot and active-window metadata. If it still
shows the old app or a transition, take at most one bounded desktop observation before replanning.

### What `verify_state` checks

Oryn calls CUA `verify_state` through `computer_wait` or `computer_act.wait_for`, using an exact
window observation. Supported checks include a window's existence/bounds or an accessible
element's existence, enabled/selected state, or exact value. For example, after typing a name:

```json
{
  "observation_id": "obs-b",
  "expect": {
    "element": {
      "selector": {"role": "textbox", "label_contains": "Search"},
      "value_equals": "Puneet"
    }
  },
  "timeout_ms": 3000
}
```

This could return `satisfied` if the accessible Search field contains `Puneet`,
`unsatisfied` if the condition is not met, or `unknown` if CUA cannot establish it.
It does not identify the correct contact or read a message; those need fresh screen evidence.
Desktop observations do not support this exact-window check.

## IDs: each names a different thing

| ID or field | Meaning and lifetime |
| --- | --- |
| `session_id` | The saved chat in SQLite. |
| `turn_id` / `round` | One user turn / one model request within that turn. |
| `trace_id` / `#0042` (`seq`) | One local diagnostic trace / one event in it. Event numbering can restart when a new trace starts in the same log file. |
| `call_...` (`call_id`) | One model tool call, linking its start to its result. |
| `action_id` / `wait_id` | One input attempt or native wait, linking it to its result and following observation. |
| `pid` + `window_id` | The process and exact CUA window to read. These can change when apps restart. |
| `observation_id` | The latest screen or windows-list observation. `computer_act` consumes it once; `computer_wait` accepts exact-window observations. A new observation replaces it. |
| `window_index` | A focusable Hyprland window in one windows-list observation. It is not stable across lists. |
| Control `[3]` (`element_index`) | A numbered control in one exact-window observation. It is not stable across observations. |
| `active_window_stamp` | Hyprland window address, monitor, position, and size. Oryn checks it before desktop input. It does not describe the page's contents. |
| `capture_id` | CUA's reference for a captured image, used with the observation's image scope and coordinates. |

## One example, from target to evidence

The IDs below are invented. Suppose Brave is on workspace 3 and Blender is on workspace 5:

1. `computer_observe({"mode":"windows"})` returns `observation_id="list-a"`, plus
   `Brave: workspace=3, window_index=1, pid=4321, is_active=false` and
   `Blender: workspace=5, window_index=2, is_active=false`. It has no screenshot. A uniquely matched
   Brave window may also have `window_id=71`.
2. `computer_act({"observation_id":"list-a","action":{"type":"focus_window","window_index":1}})`
   checks that the listed Brave window is still the same, focuses it, and returns a fresh
   screenshot and accessibility controls when CUA can match it. This is one action;
   no key or click is chained to the focus.
3. The model uses that fresh observation, perhaps containing `[3] textbox "Search chats"`,
   for its next input. Reusing `list-a` is rejected.
4. The model inspects each returned observation and can use it directly for the next action. It calls
   `computer_observe` again only if the UI is still loading or this result is insufficient;
   it can use `computer_wait` for a supported exact condition. A task with one outcome
   needs no `computer_task` record even if it takes several actions. For longer tasks,
   one `record` call can update multiple steps and checks, each with its own evidence.

The input result `sent` means the dotool command completed. The model checks the resulting
observation before claiming that the app changed. `refused` means Oryn stopped the requested
input before dispatch; `unknown` means its occurrence is uncertain.

For longer tasks, `computer_task({"operation":"record","updates":[...]})` can mark up to
eight steps and six done checks in one call. Each item names `kind` (`step` or `check`), a
1-based `index`, a valid `status`, and evidence for `verified` or `blocked`. The older
single-step and single-check fields still work for existing callers but are no longer
advertised to the model. If the next action is already clear from
the last observation, the model can issue `record` before `computer_act` in the same
response. After the final result, it can issue `record` then `finish` together. It must
never record evidence from an action it has not yet seen. The result lists remaining
step/check indices; `finish` can reuse evidence from a verified done check once both
lists are empty.

## What stays in history

The next model request receives the latest computer tool result and its attached screenshot
when one was captured. After the model has read it, Oryn removes older computer screenshots
and keeps compact text outcomes in the chat transcript. The transcript is saved in SQLite;
long chats can use a context summary. A `computer_task` record is saved separately per chat
and can carry an unfinished goal and progress into a later turn. Observation IDs and control
indices cannot be reused in that later turn: Oryn must observe again.

The optional JSONL diagnostic trace links calls, actions, and observations, but stores image
metadata rather than screenshot pixels by default. `computer-log.sh run --images` saves each
image attached to a computer tool result, linked by observation ID, plus separate marked
pre-click images in a folder for each traced turn. The live `follow` view prints the saved paths; see
[the main-loop decision record](21-cua-main-loop.md) and [computer tracing](22-computer-tracing.md).
