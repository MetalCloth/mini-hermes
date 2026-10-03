---
name: computer-cua
description: Plan Oryn /computer actions with CUA screenshots and accessibility hints on this Hyprland desktop.
---

# Oryn computer guide: CUA on Hyprland

Oryn shows you a full desktop screenshot and, when available, a short accessibility listing for the active window. The screenshot is the source for visible state. Labels can be missing, stale, or wrong; check them against the screenshot. A `[x=..., y=..., w=..., h=...]` box is in **screenshot pixels** only when Oryn has verified the window geometry. A label without a box gives no click coordinates.

Return only Oryn's `computer_plan` action objects, not CUA commands or shell commands. Available actions are `click`, `double_click`, `right_click`, `scroll`, `key`, and `type`. Click coordinates must refer to the screenshot. Oryn uses CUA for desktop capture, accessibility hints, and left clicks. Every `key` and `type` action uses dotool; AT-SPI text-field tokens are observation hints only and are not used to inject text. Right clicks and scrolling also use dotool. Verify input results in the next screenshot.

For `key`, use dotool key names: `ctrl+l` focuses a browser address bar, `super+w` opens Brave on this laptop, and `leftmeta` or `x:Super_L` taps Super alone. Never use bare `super` as a standalone key or an unprefixed XKB name such as `Super_L`. `enter`, `backspace`, `ctrl`, `alt`, `shift`, and `super` work in supported chords. Oryn preserves key-name case.

Batch only actions justified by the current screenshot. After navigation, a dialog, or any uncertain input, reobserve. A CUA click may report that delivery was attempted without proving the UI changed; verify the next screenshot before repeating it. Do not repeat uncertain typing or sending. Oryn keeps the active-window checks and the approval flow for external changes.

Use the companion `caelestia-hyprland` profile for this laptop's shortcuts; current screenshots and later user corrections take precedence.
