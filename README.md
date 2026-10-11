# Pearipherals: Apple Magic Keyboard and Magic Trackpad 2 for Windows

**Pearipherals is a free, open-source Windows tray app for Apple Magic Keyboard,
Magic Trackpad 2, and Magic Mouse 2015 (PID 0269). It adds Mac-style function
keys, three-finger trackpad gestures, battery levels, natural scrolling, and
display brightness controls in one portable EXE.**

## Try the v1.2.1 unsigned beta

**[Download v1.2.1 for Windows (unsigned EXE)](https://github.com/OnlineFix/Pearipherals/releases/download/v1.2.1-unsigned/Pearipherals-1.2.1-unsigned.exe)**
· [Release notes and SHA-256 checksum](https://github.com/OnlineFix/Pearipherals/releases/tag/v1.2.1-unsigned)

For **Windows 10/11 x64**, Apple Magic Keyboard and Magic Trackpad 2 over
Bluetooth; the optional Magic Mouse 2015 is PID `0269`. Trackpad pointer
movement and two-finger scrolling require the separately installed
[mac-precision-touchpad driver](https://github.com/imbushuo/mac-precision-touchpad).
Magic Mouse battery reads are read-only: the app tries Input report `0x90`, then
tries Feature report `0x47` only when the first path is unavailable and accepts
only a matching report ID and a plausible 1–100 percentage. The feature-report
fallback still needs physical confirmation on each Apple driver variant. Mouse
scrolling requires the separately installed Apple Wireless Mouse driver; the
tray's **Magic Mouse scrolling setup guide** opens the driver folder, but the
app does not install, replace, or modify any driver.

> **Unsigned beta:** Windows cannot verify the publisher. SmartScreen may warn;
> Smart App Control may block it without a per-app bypass. Do not disable
> system-wide security protections to run it.
> [Read the security warning](#windows-security-unsigned-builds-and-smart-app-control).

[Official website](https://onlinefix.github.io/Pearipherals/) · [Quick start](#download-and-quick-start) · [Requirements](#supported-hardware-and-requirements) · [FAQ](#faq) · [Magic Utilities comparison](#pearipherals-and-magic-utilities) · [Removal guide](#uninstall)

Use the versioned link above for this beta; GitHub's **Latest** release is still
v1.1.

## Features

| Feature | What it does |
|---|---|
| **"Tragic" Keyboard** | Mac-style function row for an Apple Magic Keyboard on Windows |
| **"Tragic" Trackpad** | Three-finger swipes or drag for Magic Trackpad 2 on Windows |
| **"Moodio" Display** | Brightness keys for Apple Studio Display and other monitors |
| **Battery monitoring** | Keyboard, Trackpad 2, and Magic Mouse 2015 battery levels and low-battery alerts |
| **Magic Mouse v2 support** | Read-only battery query using Input 0x90, with a Feature 0x47 fallback when that driver exposes it |
| **Mouse scroll setup** | Opens the Apple Wireless Mouse driver guide; driver installation stays user-controlled |
| **Portable Windows app** | One EXE, local configuration, no account, no telemetry |

## "Tragic" Keyboard: Apple function keys on Windows

| Key | Action |
|---|---|
| F1 / F2 | Display brightness down / up through "Moodio" |
| F3 | Task View (Mission Control) |
| F4 | Windows Search (Spotlight) |
| F5 | Passthrough |
| F6 | Open the Windows Snipping Tool selection overlay |
| F7 / F8 / F9 | Previous / Play-Pause / Next |
| F10 / F11 / F12 | Mute / Volume down / Volume up |

Hold **Ctrl, Alt, Shift, or Win** to send the original F-key. Shortcuts such as
Alt+F4 and Ctrl+F5 continue to work. You can turn the entire layer on or off from
the tray menu. **F6** without a modifier opens the **Windows Snipping Tool**
**selection overlay** so you can select a region. Windows handles the selected image,
copies it to the clipboard, and saves it according to the Snipping Tool **auto-save setting**.
Pearipherals does not capture or save the image itself. **Modifier+F6**
passes the original F6 key through instead.

## "Tragic" Trackpad: Magic Trackpad 2 gestures on Windows

The Magic Trackpad 2 Bluetooth driver reports contacts one at a time. That stops
Windows from reliably detecting native three-finger gestures. "Tragic" Trackpad
reassembles the Raw Input contact data and provides three modes:

- **Swipes**
  - swipe **down** to minimize all windows
  - swipe **up** to restore minimized windows
  - swipe **left or right** to open Task View
- **Drag** for macOS-style three-finger dragging and text selection
- **Off** to disable Pearipherals' custom three-finger handling

Two-finger scrolling and normal pointer movement remain with the Windows
Precision Touchpad driver. Pearipherals also has a natural-scrolling toggle.
Windows reads that setting when the trackpad connects, so reconnect the trackpad
or reboot after changing it.

## "Moodio" Display: Apple Studio Display brightness on Windows

F1 and F2 try three brightness methods in order:

1. Apple Studio Display USB HID control
2. DDC/CI for compatible external monitors
3. GPU gamma-ramp dimming as a fallback

This gives the brightness keys a useful fallback on displays that do not expose
normal DDC controls. A video-only USB-C-to-DisplayPort cable cannot carry the
Studio Display's USB control data, so Pearipherals uses the available fallback in
that setup.

## Magic Keyboard and Magic Trackpad battery levels

Pearipherals shows the Apple Magic Keyboard and Magic Trackpad battery levels as
separate entries in the tray menu. Battery polling is slow and isolated, and the
app reopens devices for each poll instead of keeping Bluetooth HID handles open
while a device sleeps. Polls run every five minutes while either device returns
battery data, backing off to fifteen minutes when neither does.

Each device with a valid current battery reading has **low-battery Windows notifications at 20% or below**, with
one **critical escalation at 5% or below**. The first fresh low reading after
launch warns too. Alerts are tracked separately for each device and combined
into one notification when both need a warning in the same poll. Repeated polls
and reconnects do not repeat a warning; a fresh reading of **25% or above** rearms
that device. Charging/fully-charged reports suppress warnings. Missing, invalid,
stale, or more-than-ten-minute-old readings never become a false 0% alert.

Notifications are best effort: Windows notification settings, Do Not Disturb /
Focus Assist, or Explorer can hide or suppress them, and Pearipherals cannot
confirm display. A reported notification exception is retried on a later poll,
not in a tight loop. The app must be running and receiving fresh battery data;
it cannot guarantee a warning before a sleeping or disconnected device dies.

## Supported hardware and requirements

- Windows 10 or Windows 11
- Apple Magic Keyboard connected over Bluetooth
- Apple Magic Trackpad 2 connected over Bluetooth
- Optional Apple Magic Mouse 2015 (Bluetooth PID `0269`)
- For trackpad pointer movement and two-finger scrolling, install the free,
  signed [mac-precision-touchpad](https://github.com/imbushuo/mac-precision-touchpad)
  driver once

Magic Mouse scrolling uses the separate Apple Wireless Mouse driver. The
application does not bundle or silently install kernel drivers; use the tray's
scroll setup guide and inspect the driver package before installing it.

The keyboard mappings, battery display, and monitor controls do not require that
trackpad driver.

## Download and quick start

1. For Magic Trackpad 2, install the mac-precision-touchpad driver.
2. Read the [v1.2.1 unsigned beta release notes](https://github.com/OnlineFix/Pearipherals/releases/tag/v1.2.1-unsigned)
   for security warnings, checksums, and update/rollback instructions, then use
   the [beta download above](#try-the-v121-unsigned-beta).
3. Put it anywhere you control, such as `C:\Tools\Pearipherals`.
4. Run it. Pearipherals enables autostart and applies the touchpad settings its
   gestures need on first launch, then shows a notification explaining what
   changed and where to undo it.

Pearipherals is portable. Its configuration and optional error log sit beside
the EXE as `pearipherals.json` and `pearipherals.err.log`.

## Help test Pearipherals

Pearipherals needs reports from Apple hardware beyond the maintainer's setup.
If you try the beta, submit a short [compatibility report](../../issues/new?template=compatibility_report.yml),
including when everything works. A working setup is as useful as a failed setup
because it tells us which device revisions and connection paths are reliable.
The form asks only for product-level details and warns against sharing serial
numbers, Bluetooth addresses, raw HID data, or other private information.

## Windows security, unsigned builds, and Smart App Control

An **unsigned** prerelease has no Authenticode signature, so Windows cannot
verify its publisher identity. Windows may identify it as an unknown or
untrusted publisher, Microsoft Defender SmartScreen may warn about it, and Smart
App Control may block it completely. Smart App Control does not offer a per-app
**Run anyway** exception. Do not disable a system-wide Windows security feature
just to run an unsigned build.

This warning is about publisher identity and software reputation; it is not by
itself a malware verdict. Pearipherals is public so you can inspect the source
and build it locally. Official release provenance is documented in the
[Code signing policy](CODE_SIGNING_POLICY.md).

The project applied for free open-source signing through SignPath Foundation.
The application was not accepted because Pearipherals is still new and does not
yet have enough established public usage. We plan to apply again after the user
base grows. Until trusted signing is available, every downloadable binary will
be labeled accurately. New unsigned downloads published under this policy are
GitHub prereleases accompanied by a SHA-256 checksum. The checksum can detect a
file mismatch or corruption; it does not authenticate the publisher, establish
that the program is safe, or substitute for a digital signature.

## App behavior and safety

- The tray menu controls the function-key layer, gesture mode, natural
  scrolling, autostart, and touchpad setting recovery.
- **Quit** stops input hooks, Raw Input handling, and battery polling.
- Original Windows touchpad values are backed up before Pearipherals changes
  them and can be restored from the tray.
- A single-instance guard makes accidental double launches safe.
- Startup failures are retried and written to `pearipherals.err.log`.
- Moving the EXE is supported: run it once from the new location and the
  autostart path updates.
- Pearipherals has no account, analytics, telemetry, or automatic uploads. See
  the [privacy policy](PRIVACY.md).

## Check status, get help, and prepare removal

All of this lives in the tray menu and none of it does anything until you click
it.

- **About / status…** shows the exact version, the full build identity, whether
  this is a frozen or source build, your Windows version, and the current state
  of the configuration file, battery paths, `AmtPtpHidFilter` service
  registration, Raw Input listener, gesture mode, touchpad settings, scroll
  direction, autostart, and removal readiness. Every value is a fixed label. It
  reports what Pearipherals can currently observe — a sleeping Bluetooth device
  simply reads as *not detected*, never as broken or missing.
- **Save a diagnostic report** writes `pearipherals-diagnostics.json` next to
  the executable so you can read it before sending it anywhere. It contains a
  fixed whitelist of the same product-level labels shown in About / status:
  **no local paths, no usernames, no Bluetooth or HID identifiers, no registry
  values, no raw configuration, and no log contents.**
  Pearipherals never uploads it — sharing it is entirely your decision.
- **Open documentation** and **Report a problem** open fixed project links in
  your browser, and only when clicked. There is no background update check.
- **Prepare for removal…** asks for confirmation first (**No** is the default
  button), then turns off the Mac F-row and three-finger gestures, releases any
  in-flight gesture, removes and verifies every start-with-Windows entry
  including legacy names, and restores your backed-up Windows touchpad
  settings. It reports exactly which steps completed; a partial run is never
  reported as success. It **does not delete** any file and does not quit the
  app — you choose **Quit** when you are ready.

When you upgrade, Pearipherals records the previous version and the current one
in its local configuration so About / status can show that a version change was
observed. Nothing about that is transmitted.

## Upgrade from MagicSuite

Run Pearipherals once from its new location. It imports the old
`magicsuite.json` settings and removes the legacy autostart entry. You can then
delete the old MagicSuite EXE.

## Uninstall

1. Select **Prepare for removal…** and confirm. This reverses every persistent
   change in one step and tells you if anything could not be completed.
   Resolve any reported restoration failure before deleting your settings backup.
   You can still do it by hand instead: untick **Start with Windows**, set the
   3-finger gesture to **Off**, then use **Touchpad settings** →
   **Restore original Windows settings**.
2. Select **Quit**.
3. Delete the EXE (`Pearipherals-1.2.1-unsigned.exe` for this beta, or
   `Pearipherals.exe`) and its adjacent runtime files, if present:
   `pearipherals.json`, `pearipherals.err.log`, and
   `pearipherals-diagnostics.json`.

This removes Pearipherals, its autostart entry, and its local configuration/log.
The Restore action replays the Windows touchpad values backed up before
Pearipherals first managed them.

The separately installed trackpad driver is not removed by deleting Pearipherals.

## Build Pearipherals from source

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
set PEARIPHERALS_VERSION=1.2.1
build.bat
```

The portable Windows executable is written to `dist\Pearipherals.exe`. The
release workflow uses a complete hash-locked dependency set and adds Windows
product/version metadata before signing submission.

## How Pearipherals works

- **Keyboard function row:** a low-level keyboard hook (`WH_KEYBOARD_LL`)
  intercepts F1-F12 and injects the chosen media, brightness, or Windows action.
  Modifier-held presses pass through untouched. Injected events are tagged so
  the hook cannot loop.
- **Display brightness:** Apple Studio Display USB HID is tried first, followed
  by DDC/CI through `dxva2.dll`, then GPU gamma-ramp dimming.
- **Trackpad gestures:** Raw Input is registered for the Precision Touchpad usage
  (`0x0D/0x05`). Pearipherals parses contact data through `HidP_*`, filters
  padding and stale contacts, and feeds stable contacts into its gesture state
  machine. A low-level mouse hook suppresses leaked pointer motion only while a
  valid custom gesture owns the input.
- **Battery monitoring:** Apple vendor HID battery reports are queried separately
  for the keyboard and trackpad, then published to the Windows tray thread.

Pearipherals uses no kernel code and does not require administrator rights.

## Pearipherals and Magic Utilities

Looking for a free Magic Utilities alternative? Pearipherals is a free,
MIT-licensed companion for the documented Magic Keyboard and Magic Trackpad 2
Bluetooth setup on Windows 10/11 x64. It provides function-key controls, custom
three-finger gestures and battery monitoring, but it is **not a driver or a
feature-for-feature replacement**. Trackpad pointer movement and two-finger
scrolling require the separate mac-precision-touchpad driver. The current beta
is unsigned and hardware coverage is still being validated.

[Magic Utilities](https://magicutilities.net/) provides its own Windows drivers
and documents Bluetooth and wired USB support, including Magic Mouse support.
Check its official device and feature documentation if you need that broader
scope. Pearipherals does not claim Magic Mouse support or validated wired USB
input. Choose based on your exact hardware, required features and security
requirements; do not assume the two products are interchangeable.

## FAQ

### Does Apple Magic Trackpad 2 work on Windows 11?

The documented setup is Magic Trackpad 2 over Bluetooth on Windows 10/11 x64, with the separately installed mac-precision-touchpad driver for pointer movement and two-finger scrolling. Pearipherals adds custom three-finger gestures, natural-scrolling control and battery display. Not every hardware revision or setup has been validated.

### How do I get Apple Magic Keyboard function keys on Windows?

Pearipherals gives supported Bluetooth Magic Keyboards Mac-style brightness, Task View, Search, Snipping Tool, media and volume actions. F5 passes through unchanged. Hold Ctrl, Alt, Shift or Win to send the original F-key. The Fn key is not exposed over this Bluetooth path.

### How can I check Magic Keyboard and Magic Trackpad battery levels on Windows?

Open the Pearipherals tray menu to see separate keyboard and trackpad battery readings when supported devices provide data. Low-battery notifications trigger on fresh readings at 20% or below, with one critical escalation at 5% or below. Warnings are best effort, not a guarantee before a battery dies.

### Why is a battery reading missing or a low-battery alert not showing?

Sleeping or disconnected devices may not provide fresh data. Missing or stale readings are not treated as zero. Windows notification settings, Do Not Disturb and Explorer can suppress alerts. Check About / status in the tray; the app must be running and receiving fresh readings.

### Is Pearipherals a free, open-source alternative to Magic Utilities?

Pearipherals may suit people looking for free function-key controls, custom three-finger gestures and battery monitoring for the documented Bluetooth setup. It is MIT-licensed, with no paid tier. It is not a feature-for-feature replacement or a trackpad driver: Magic Trackpad 2 still needs the separate mac-precision-touchpad driver. The current Pearipherals beta is unsigned and not every hardware revision has been validated.

### Does Pearipherals replace a Windows Magic Trackpad driver?

No. It is a companion utility and does not bundle or replace mac-precision-touchpad. Install that driver separately for Magic Trackpad 2 pointer movement and two-finger scrolling. Keyboard mappings, battery display and monitor controls do not require the trackpad driver.

### Are USB-C models, wired USB and Windows on ARM supported?

The documented setup is Bluetooth on Windows 10/11 x64. USB-C hardware revisions, wired USB input and Windows on ARM are not claimed as validated. Do not assume support from the Magic Keyboard or Magic Trackpad product name alone.

### Can I use three-finger gestures and change scrolling direction?

Choose Swipes for down to minimize, up to restore and left/right for Task View; choose Drag for three-finger dragging and text selection, or Off. The natural-scrolling toggle requires a trackpad reconnect or reboot. Four-finger gestures are not handled.

### Can I control Apple Studio Display brightness on Windows?

Pearipherals tries Apple Studio Display USB HID control, then DDC/CI for compatible monitors, then GPU gamma-ramp dimming. Results depend on the display and connection. A video-only USB-C-to-DisplayPort cable carries no USB control data; gamma dimming is a software fallback, not native backlight control.

### Why does Windows show an unknown publisher or block the EXE?

The current v1.2.1 beta is unsigned, so Windows cannot verify its publisher. SmartScreen may warn and Smart App Control may block it without a per-app exception. Do not disable system-wide security protections. Public source and a SHA-256 checksum do not replace a trusted digital signature or guarantee safety.

### Does Pearipherals collect data or require an account?

No account is required. The app has no telemetry or automatic uploads. Optional diagnostic reports are saved locally only when requested, for you to review before choosing to share them.

### How do I uninstall Pearipherals and undo its changes?

Use Prepare for removal from the tray and inspect every result. Resolve restoration failures before deleting your settings backup, then choose Quit and delete the EXE and adjacent runtime files. First launch enables autostart and applies touchpad settings; removal restores the backed-up settings and removes startup entries. The separate trackpad driver is not removed.

### Where can I report a working setup or a problem?

Use the GitHub compatibility report form for working or failing setups, or the bug report form for a problem. Include your Windows version, product model, connection type and what worked or failed. Do not share serial numbers, Bluetooth addresses or raw HID data; review any diagnostics before posting.

[Report compatibility](https://github.com/OnlineFix/Pearipherals/issues/new?template=compatibility_report.yml) · [Report a bug](https://github.com/OnlineFix/Pearipherals/issues/new?template=bug_report.yml)

## Known limitations

- Natural-scroll changes require a trackpad reconnect or reboot because Windows
  caches the setting when the device initializes.
- Four-finger gestures are not handled yet.
- The Magic Keyboard Fn key is not exposed to Windows over this Bluetooth path,
  so Pearipherals uses modifiers to access the original F-keys.
- Trusted public code signing is not available yet.

## Project links

- [Releases](../../releases)
- [Code signing policy](CODE_SIGNING_POLICY.md)
- [Privacy policy](PRIVACY.md)
- [MIT license](LICENSE)

---

Pearipherals is not affiliated with, endorsed by, or sponsored by Apple Inc.
Apple, Magic Keyboard, Magic Trackpad, and Studio Display are trademarks of
Apple Inc. and are used only to describe supported hardware.
