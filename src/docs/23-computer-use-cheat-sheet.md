# Computer-use cheat sheet

This is a quick reference for Oryn's current computer tools in regular TUI chat. The model calls
`computer_*` tools; by default, Oryn uses CUA to read the local desktop and dotool to send input.
Native `browser_*` tools control a separate remote Firecrawl browser, not local Brave.
With `ORYN_COMPUTER_DRIVER=dotool`, only desktop screenshots and dotool input are available;
exact CUA windows, accessibility controls, and native checks are unavailable.

## The short version

| Part | Job |
| --- | --- |
| `computer_task` | Saved checklist: goal, constraints, steps, done checks, progress, and evidence. It does not control the screen or prove success. |
| `computer_observe` | Look at the desktop, a list of windows, or one window. It does not send input. |
| CUA | Read windows, screenshots, accessibility data, and exact native conditions. |
| `computer_act` + dotool | Send a click, key, text, or scroll, then return a fresh observation. |
| `computer_wait` | Wait for one supported CUA condition in an exact window, then return a fresh observation. |
| `computer_ask_user` | Ask for a clarification during the same turn. |

## Four ways to observe

| Model call | CUA read | What the model receives |
| --- | --- | --- |
| `computer_observe({"mode":"windows"})` | `list_windows` | Visible app/window metadata: `pid`, `window_id`, title, app name, bounds. No screenshot, accessibility tree, or actionable observation ID. |
| `computer_observe({"mode":"active"})` | Match the focused Hyprland window with `list_windows`, then `get_window_state` | That window's screenshot and bounded accessibility controls/tree when a unique match exists. Otherwise a desktop screenshot. |
| `computer_observe({"mode":"window","pid":4321,"window_id":71})` | `get_window_state` | The specified window's bounded controls/tree and a screenshot when available. On Wayland, Oryn may fall back to a desktop screenshot while keeping the exact window's tree. |
| `computer_observe({"mode":"desktop"})` | `get_desktop_state` | Full-screen screenshot, plus read-only labels from the active window when CUA can safely match it. These labels are not numbered click targets. |

`start_session` and `end_session` prepare and close CUA; they are not screen reads.
The accessibility tree is what an app exposes to the desktop, not a direct browser DOM feed.
Some apps expose only a generic window. Window and active observations send at most 60 actionable
controls and a 1,200-character tree excerpt to the model.
`mode="windows"` has no image; `mode="desktop"` always has one. `image=false` can skip an
exact-window screenshot, but an active-mode fallback can still return a desktop image.

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
| `observation_id` | The latest screen snapshot. `computer_act` or `computer_wait` consumes it once; a new observation replaces it. |
| Control `[3]` (`element_index`) | A numbered control in one exact-window observation. It is not stable across observations. |
| `active_window_stamp` | Hyprland window address, monitor, position, and size. Oryn checks it before desktop input. It does not describe the page's contents. |
| `capture_id` | CUA's reference for a captured image, used with the observation's image scope and coordinates. |

## One example, from target to evidence

The IDs below are invented. Suppose Brave is open on WhatsApp Web:

1. `computer_observe({"mode":"windows"})` returns `Brave: pid=4321, window_id=71`.
   This is a directory entry, with no screenshot.
2. `computer_observe({"mode":"window","pid":4321,"window_id":71})` returns
   `observation_id="obs-a"`, a screenshot, and perhaps `[3] textbox "Search chats"`
   if Brave exposes that control.
3. `computer_act` uses `obs-a` for an input. Clicks and keys outside a small navigation
   allowlist require approval; after approval Oryn reobserves before input. When input runs,
   the result includes a new observation, say `obs-b`. Reusing `obs-a` is rejected.
4. The model inspects `obs-b`, or uses `computer_wait` with a supported exact condition.
   It records a verified step in `computer_task` only when the observed evidence supports it.

The input result `sent` means the dotool command completed. The model checks the resulting
observation before claiming that the app changed. `refused` means Oryn stopped the requested
input before dispatch; `unknown` means its occurrence is uncertain.

## What stays in history

The next model request receives the latest computer tool result and its attached screenshot
when one was captured. After the model has read it, Oryn removes older computer screenshots
and keeps compact text outcomes in the chat transcript. The transcript is saved in SQLite;
long chats can use a context summary. A `computer_task` record is saved separately per chat
and can carry an unfinished goal and progress into a later turn. Observation IDs and control
indices cannot be reused in that later turn: Oryn must observe again.

The optional JSONL diagnostic trace links calls, actions, and observations, but stores image
metadata rather than screenshot pixels. For implementation details, see
[the main-loop decision record](21-cua-main-loop.md) and [computer tracing](22-computer-tracing.md).
