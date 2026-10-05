# CUA Driver on Hyprland: local A/B experiment

**Run date:** 3 October 2026

**CUA Driver:** `cua-driver-rs` 0.32.0
**Scope at the time:** Compare CUA with Oryn's then-current screenshot + optional AT-SPI + dotool path, without changing production code or defaults during the isolated experiment. Subsequent integration and the current TUI architecture are recorded below and in the [main-loop decision record](21-cua-main-loop.md).

## Setup

CUA was downloaded to `/tmp/cua-driver-experiment-v0.32.0`; it was not installed system-wide. The archive SHA-256 matched the checksum published with the [v0.32.0 release](https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.32.0):

```text
998e63452c38b76a682da2d07f4bb24f0c1663d76c861a5dc0c345a7ead1f889
```

The test used CUA's native Wayland opt-in, `CUA_DRIVER_RS_ENABLE_WAYLAND=1`; without it, the driver did not enumerate the Wayland windows in this session. CUA ran with a temporary home/config directory, standard permissions, telemetry disabled, and no overlay. Oryn's AT-SPI path was enabled only for the test process with `ORYN_COMPUTER_ATSPI=1`.

Both paths used the same `run_computer()` loop, `gpt-5.6-luna`, low reasoning effort, priority service tier, a 45-second request timeout, and a disposable GTK3 window on an otherwise empty Hyprland workspace. A guard checked the fixture PID/title before each input. The task never touched another app. The fixture was restarted for each run.

The desktop environment had `NO_AT_BRIDGE=1`; that variable was removed only from the fixture process so GTK exposed its AT-SPI bridge. It was not changed globally.

## Results

### Read-only task

Task: read the fixture's status label exactly and make no changes.

| Driver | Model calls / turns | Input actions | Total elapsed | Model response | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Oryn + dotool + AT-SPI | 1 / 1 | 0 | 3.85 s | 2.81 s | Correct: `Result: empty`; fresh CUA tree agreed |
| CUA Driver | 1 / 1 | 0 | 4.93 s | 4.22 s | Correct: `Result: empty`; fresh CUA tree agreed |

This single read-only pair shows both paths could answer from the visible local window. It does not establish a speed difference; the model response itself took longer in the CUA run.

### Safe interaction task

Task: enter `ORYN-CUA-32` in the fixture's test-token field, submit it, and stop when the matching result is visible.

| Driver | Model calls / turns | Input actions | Total elapsed | Median model response | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Oryn + dotool + AT-SPI | 8 / 8 | 3 | 53.46 s | 4.41 s | Completed; fresh CUA tree read `Result: ORYN-CUA-32` |
| CUA Driver | 4 / 4 | 2 | 22.72 s | 4.07 s | Completed; fresh CUA tree read `Result: ORYN-CUA-32` |

Both retained trials kept the fixture as the active window, dispatched no action to another app, and reached the exact result. In the CUA run, the model's first action typed into the named accessibility field; the dotool plan first clicked the field and then typed. This is promising evidence for semantic targeting, but the planner's choices and turn count varied between runs.

An earlier exploratory interaction pair also completed on both paths with four model calls each. Its logs were replaced during the later verifier correction, so it is not included in the retained metrics above. That variation is another reason not to treat one fast CUA result as a general speed claim.

## What worked and what did not

- CUA's native Wayland mode found the isolated GTK window and returned its accessibility elements together with a window screenshot. Oryn's then-current dotool path also supplied a screenshot and two AT-SPI labels for this fixture.
- CUA could target the entry semantically by element token. A separate direct diagnostic reported text entry as `confirmed`; its click result was `unverifiable` until a fresh state read proved the displayed result. A successful dispatch alone is not proof that an action worked.
- The CUA screenshot was smaller than the window's native geometry. Its state included `frame_scale` (observed as about `0.79`); element frames had to be scaled before matching them to screenshot pixels. The first temporary adapter omitted this conversion and missed the button. That was an experiment-adapter bug, not a CUA driver failure. With the mapping corrected, semantic targeting worked.
- CUA's Wayland path required an explicit environment flag. This test does not show that the default configuration works, that every compositor route is supported, or that arbitrary background input is available. See the [Linux driver guide](https://github.com/trycua/cua/blob/main/libs/cua-driver/rust/Skills/cua-driver/LINUX.md) and [action support ledger](https://github.com/trycua/cua/blob/main/libs/cua-driver/docs/action-support.md) for the project's stated platform boundaries.
- The observed time difference is mostly inseparable from model/network variation and planner decisions. In the retained interaction pair, the model used twice as many turns on dotool; the earlier pilot used four turns on each path. One read-only call per path is far too little to compare latency.

## Recommendation at the time of the experiment

Keep dotool as Oryn's default for now. CUA is worth further isolated testing as an optional observation/action route: this trial shows useful semantic field targeting and successful fresh-state verification on one GTK3 Wayland window. It does not prove a general speed-up or reliable behavior in Chromium, Electron, Discord, or other Hyprland apps.

Before considering adoption, repeat the same safe tasks several times in alternating order across GTK, Zen/Firefox, and Chromium/Electron, recording wrong-target actions, refusals, stale-element behavior, focus changes, turns, and elapsed time. Use blank/test pages and disposable fields; do not test by sending real messages. Any production integration would be a separate architecture decision and is not part of this experiment.

## Subsequent decision and implementation

The user subsequently chose CUA as Oryn's default `/computer` observation driver. The initial integration used CUA's semantic `type_text` when it found one safe accessibility field, while dotool handled keyboard shortcuts and other unsupported actions. On 2026-10-03, a live browser task showed that CUA's `type_text` route refused input with `foreground_unavailable: production Hyprland input plugin is unavailable`; dotool's hotkey fallback worked. The then-current routing kept CUA for screenshots, accessibility observations, and left clicks, and sent every keypress and text entry through dotool. That was the active setup before the later click-routing change. The AT-SPI tree remains an observation hint, not an injection route. `ORYN_COMPUTER_DRIVER=dotool` selects the all-dotool screenshot/input driver. This change does not turn the single-window timing result above into a general speed claim. The earlier isolated GTK fixture check did verify that CUA semantic typing could work in that specific test environment, but it did not establish that CUA's Hyprland keyboard route is available in normal use.

A disposable Brave app window exposed a CUA accessibility tree, but its first-run "Can't update Brave" alert repeatedly took focus during the typing check. The window guard stopped or the field lacked a usable token, so that run did not verify browser text entry. The current dotool keyboard route avoids that CUA call, but it has not yet been checked end-to-end through `/computer` in Brave; a successful standalone dotool test is not the same verification.

## 4 October: numbered controls in Oryn

At this stage of the earlier planner integration, Oryn retained actionable CUA `element_index`/`element_token` pairs from each active-window snapshot. The model saw compact `[N] role: label` entries and could return `click_element` for a number in the **current** observation. Oryn validated that number before input, then sent the matching token scoped to the captured window through CUA. A new screenshot discarded the old mapping. Coordinate clicks remained available when the tree was sparse. Typing and hotkeys still used dotool. The [current main-loop tools](21-cua-main-loop.md) also bind numbered controls to a fresh observation, but present them through a different tool result.

Live local checks on the 1920×1080 Hyprland desktop:

| App | CUA observation | Input check | Result |
| --- | --- | --- | --- |
| Brave | Numbered controls included its address bar; one snapshot had 23 usable controls | Activated the address-bar entry by element token through the full Oryn loop and a deterministic test planner | Two harness turns completed; a separate AT-SPI check changed from `focused=false` to `focused=true`; the prior focused window was restored |
| Spotify | `get_window_state` returned only a frame and zero numbered controls | Used screenshot coordinates to select Music, then All, through the same loop and test planner | Three harness turns completed after visual state checks; the All filter and prior focused window were restored |

One Brave snapshot immediately after a focus switch omitted the address bar; the next fresh read exposed it. Spotify coordinate clicks reported `global_input` with an `unverifiable` effect. Short verification intervals sometimes observed the old filter before its UI settled, so the successful harness check waited for visible selection changes before continuing. These checks validate the local CUA routes and fallback. They do not measure model speed or prove that the selected model will choose the best control on every page. The historical A/B timing table above used a separate disposable GTK fixture and should not be read as a Brave or Spotify benchmark.

## 5 October: Dotool owns clicks in `/computer`

The user changed the active `/computer` routing so CUA cannot dispatch clicks. Recent local browser-click traces showed CUA attempting the `global_input` route and returning an unsupported-operation refusal; coordinate and semantic CUA click attempts therefore added failure and retry complexity without providing a dependable input path. The current `ComputerSession` uses CUA only for window lists, accessibility/screenshot observations, and native `verify_state`; Dotool sends every click, key, text entry, right click, and scroll.

For `click` and `double_click`, the model supplies a point from the latest screenshot. For `click_element`, Oryn reads the selected control's frame from that same observation, maps its center through the screenshot/window bounds and selected-monitor scale, then sends that point through Dotool. If the frame or coordinate mapping is missing or unsafe, Oryn returns `refused` before sending the requested click; the model can request an image-backed observation and use visible screenshot coordinates. Dotool's result remains `unverifiable` until a fresh observation shows the UI effect. CUA still may verify an exact native predicate after the click, but it never performs the click itself.

## Artifacts and scope

The temporary binary, harnesses, and retained JSONL traces are in `/tmp/cua-driver-experiment-v0.32.0`. During this isolated experiment, no Oryn production code, configuration defaults, or provider loop were changed. Pre-existing workspace edits were left untouched. No commit or push was made. Those scope statements describe the experiment, not the later CUA integration or the [current TUI `/computer` flow](21-cua-main-loop.md).
