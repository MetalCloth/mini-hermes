---
name: caelestia-hyprland
description: Use for Oryn /computer tasks on the user's Caelestia-style Hyprland desktop, especially launcher, app, workspace, and window shortcuts.
---

# Caelestia Hyprland desktop profile

Use this profile when planning desktop actions for this user. It is based on the public Caelestia dotfiles and shell READMEs because the user says their laptop uses the same design. These are upstream documented defaults, not a live read of the user's config. Screenshots and explicit user corrections override the profile. Apps and keybinds may be customized in `hypr-vars.lua` and `hypr-user.lua`.

Sources:
- https://github.com/caelestia-dots/caelestia#default-keybinds
- https://github.com/caelestia-dots/caelestia#configuring
- https://github.com/caelestia-dots/shell

## User-confirmed laptop overrides

These current bindings override the upstream defaults below:

- `Super+W` opens Brave. The upstream default app is Firefox; use Brave for this laptop and verify the resulting window.
- `Super+Alt+F` fullscreen the active window. The upstream README describes this as bordered fullscreen.
- `Super+F` focuses or isolates the current screen/window, as the user described. Its precise Hyprland mode is not specified; do not treat it as the fullscreen shortcut.

## Super key and app launching

- `Super` means the Linux Super/Windows-logo key. Tapping and releasing it by itself opens the Caelestia launcher.
- For Oryn's dotool driver, send a standalone Super tap as `leftmeta` (or `x:Super_L`); `super` is for a modifier chord, not a standalone key.
- After opening the launcher, take a fresh screenshot. Type an app name only after the screenshot shows the launcher and its input is ready; inspect results before selecting one.
- On this laptop, `Super+W` opens Brave per the user-confirmed override above. Check the screenshot to verify it actually opened.
- `Super+T` opens the terminal (default foot); `Super+C` the editor (default Codium); `Super+E` the file explorer (default Thunar). These apps can also be overridden.
- For a specific app, use its shortcut only when appropriate; otherwise open the launcher, search, and verify the result.

## Default shortcuts

### Workspaces

| Shortcut | Action |
| --- | --- |
| `Super + 1…9, 0` | Go to workspaces 1–10 |
| `Super + Alt + 1…9, 0` | Move the active window to workspaces 1–10 |
| `Ctrl + Super + 1…9, 0` | Go to workspace groups (×10) |
| `Ctrl + Super + Alt + 1…9, 0` | Move the active window to a workspace group |
| `Super + Scroll Down` / `Super + Page_Down` / `Ctrl + Super + Right` | Next workspace |
| `Super + Scroll Up` / `Super + Page_Up` / `Ctrl + Super + Left` | Previous workspace |
| `Ctrl + Super + Scroll Down/Up` | Next/previous workspace group |
| `Super + Alt + Scroll/Page_Down` / `Ctrl + Super + Shift + Right` | Move active window to next workspace |
| `Super + Alt + Scroll/Page_Up` / `Ctrl + Super + Shift + Left` | Move active window to previous workspace |
| `Super + Alt + S` / `Ctrl + Super + Shift + Up` | Move active window to special workspace |
| `Ctrl + Super + Shift + Down` | Move active window out of special workspace |

### Window groups

| Shortcut | Action |
| --- | --- |
| `Alt + Tab` / `Shift + Alt + Tab` | Next/previous window in group |
| `Ctrl + Alt + Tab` / `Ctrl + Shift + Alt + Tab` | Next/previous window group |
| `Super + U` | Remove active window from group |
| `Super + Comma` | Toggle window grouping |
| `Super + Shift + Comma` | Lock active group |

### Window control

| Shortcut | Action |
| --- | --- |
| `Super + Arrow` | Focus window in that direction |
| `Super + Shift + Arrow` | Move active window in that direction |
| `Super + Minus` / `Super + Alt + Left` | Decrease window width |
| `Super + Equal` / `Super + Alt + Right` | Increase window width |
| `Super + Shift + Minus` / `Super + Alt + Up` | Decrease window height |
| `Super + Alt + Down` | Increase window height |
| `Super + LMB drag` / `Super + Z + LMB` | Move window |
| `Super + RMB drag` / `Super + X + LMB` | Resize window |
| `Ctrl + Super + Backslash` | Center window |
| `Ctrl + Super + Alt + Backslash` | Resize to 55×70% and center |
| `Super + Alt + Backslash` | Picture-in-picture mode |
| `Super + P` | Pin window |
| `Super + F` | Focus/isolate current screen or window (user description; exact mode unspecified) |
| `Super + Alt + F` | Fullscreen active window (user-confirmed laptop override) |
| `Super + Alt + Space` | Toggle floating mode |
| `Super + Q` | Close active window |

The upstream README lists `Super + Shift + Minus` for both decreasing and increasing window height. Treat the increase binding as a documentation typo; use the documented `Super + Alt + Down` alternative or verify on screen.

### Special workspaces and apps

| Shortcut | Action |
| --- | --- |
| `Super + S` | Toggle special workspace |
| `Ctrl + Shift + Escape` | Toggle system-monitor workspace |
| `Super + M` / `Super + D` / `Super + R` | Toggle music / communication / todo workspace |
| `Super + T` / `Super + W` / `Super + C` / `Super + E` | Terminal / browser / editor / file explorer |
| `Ctrl + Alt + V` | Audio settings (default `pwvucontrol`) |

### Utilities, media, and shell

| Shortcut | Action |
| --- | --- |
| `Print` | Screenshot |
| `Super + Shift + S` | Screenshot freeze |
| `Super + Shift + Alt + S` | Region screenshot |
| `Ctrl + Alt + R` | Fullscreen recording |
| `Super + Alt + R` | Recording with sound |
| `Super + Shift + Alt + R` | Region recording |
| `Super + Shift + C` | Color picker |
| `Ctrl + Super + Space` | Play/pause media |
| `Ctrl + Super + Equal` / `Ctrl + Super + Minus` | Next/previous track |
| `Ctrl + Super + Backspace` | Stop playback |
| `Super + Shift + M` | Mute volume |
| `Ctrl + Alt + Delete` | Open shell session menu |
| `Super + N` | Toggle shell sidebar |
| `Ctrl + Alt + C` | Clear shell notifications |
| `Super + K` | Show all shell panels |
| `Super + L` | Lock screen |
| `Super + Alt + L` | Restore shell lockscreen |
| `Super + Shift + L` | Run sleep command |
| `Super + V` / `Super + Alt + V` | Clipboard history / delete mode |
| `Ctrl + Shift + Alt + V` | Paste latest clipboard entry |
| `Super + Period` | Emoji picker |
| `Ctrl + Super + Alt + R` / `Ctrl + Super + Shift + R` | Restart / kill shell |

Treat sleep, lock, and shell-session actions as intentional system operations: perform them only when the user asks for that outcome.

## Shell features visible on screen

- The bar can show workspaces, active window, tray, clock, network, Bluetooth, battery, and power controls.
- The dashboard can show media, weather, and performance values such as battery, GPU, CPU, memory, storage, and network.
- On this laptop, moving the pointer into the very top-edge hotspot automatically expands the dashboard panel, notch-style; it is an edge-triggered panel, not a click-to-open control or a tooltip. Its Dashboard, Media, Performance, and Weather tabs run across the top.
- A separate narrow control pill appears at the top-right edge. The screenshot shows a speaker slider, a moon-marked slider, and a sun button. Identify their current state from the screen; do not guess the moon control's exact effect.
- The `/computer` action schema has no standalone pointer-move action, so it cannot deliberately reach this hotspot without another action. Use the panel's controls when it is already visible, or use another visible route.
- The launcher searches apps and exposes actions. The shell README documents `>wallpaper` to open the wallpaper switcher; prefixes and available actions can be customized.

## Operating rule

Use the shortcut map to plan one appropriate action at a time, then verify the result from a fresh screenshot. If a shortcut does not work, do not repeat it blindly or invent a replacement; inspect the screen and use a visible control or ask the user when the active binding is uncertain. For dotool key syntax, action limits, focus behavior, and execution boundaries, follow the `computer-dotool` skill.
