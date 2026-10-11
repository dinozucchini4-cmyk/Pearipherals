# Own Magic Utilities — Integration Plan

**Status:** engineering plan, not a claim that a combined build is complete.

This branch is a safe integration workspace. It does not modify the default branch, install drivers, edit device registry entries, or redistribute third-party driver binaries.

## Target hardware and success criteria

| Device | Identifier | Required outcome |
|---|---|---|
| Magic Mouse 2015 | Apple VID 004C, Bluetooth PID 0269 | pointer, click, horizontal/vertical scroll, battery level |
| Magic Trackpad 2 | Apple VID 004C, Bluetooth PID 0265 | pointer, click, Windows Precision Touchpad scrolling, gestures, battery level |
| Magic Keyboard | Apple VID 004C, Bluetooth PID 0267 | normal keys, existing optional function-key customization, battery level |

Battery health/design capacity must be shown as N/A unless a hardware interface actually reports those values. Missing reports must never be shown as 0%.

## Repository findings

1. `MagicTrackpad2ForWindows` is the upstream Magic Trackpad 2 precision driver project. Its README states Bluetooth, battery reporting, haptics and a control panel are supported; installation guidance is Windows 11 focused.
2. `magic-trackpad-windows` is a lifecycle/installer/diagnostics wrapper around that driver. It emphasizes exact Driver Store identification, signed payload verification, safe removal, and Windows 11 x64 validation. Use as a model for safe driver lifecycle handling, not as proof the app's battery monitor can read every driver stack.
3. `Pearipherals` already has user-mode code for keyboard shortcuts, Raw Input parsing for custom three-finger gestures, independent HID battery polling, a tray interface, configuration recovery, logging, and tests. Its current public upstream project documents Windows 10/11 x64 and Magic Keyboard/Trackpad Bluetooth, but says not every hardware revision is validated. It does not implement Magic Mouse scrolling.
4. `MagicMouseWindows` is for Magic Mouse USB-C/2024 PID 0323 and requires Magic Utilities' proprietary driver. It is not the right target for the user's 2015 PID 0269.
5. `magic-mouse-v3-windows-fix` explicitly targets PID 0323, not PID 0269. Do not apply that binary patch to PID 0269.
6. `Apple-Input-Device-Drivers-Windows-10-11` contains repackaged Apple/Boot Camp driver installers. Use for reference/installation only after checking hardware IDs and package signatures. Do not stack multiple mouse filters.
7. `magickeyboard` is a Boot Camp Apple keyboard driver/layout repository, not the complete combined app.
8. `paata` is currently empty.

## Known machine evidence

The user's Windows 11 x64 machine has previously demonstrated these mutually exclusive Mouse states:

- Magic Utilities MagicMouse.sys: pointer/click/battery work; scrolling does not in the Own Magic app.
- AppleWirelessMouse.sys: pointer/click/scroll work; the current Own Magic HID battery query does not.
- Stacking both in LowerFilters: scrolling and battery both failed.

The user's current battery implementation reads Apple report ID 0x90 from a matching vendor-defined HID collection (Usage Page 0xFF00, Usage 0x14), with battery percent in byte 2 for the successful keyboard case. Under Apple Mouse and Precision Trackpad drivers, the scanner previously saw no 0x90 capability on the accessible collections. This makes preserving one driver stack and independently obtaining device battery data a core integration requirement.

## Proposed application architecture

- Keep an app/UI layer and device services separate.
- Reuse Pearipherals' tested-in-repository patterns for tray lifecycle, keyboard customization, raw-input PTP contact processing, safe configuration persistence and per-device battery polling after compatibility review.
- Integrate the existing Own Magic Utilities battery implementation as a separate backend, and retain both backends behind an interface until real-device tests establish which driver paths answer on this machine.
- Mouse scrolling for PID 0269 remains its own workstream: either consume a proven touch/raw interface from a suitable driver or design a clean-room KMDF filter only after the required protocol is confirmed. Do not invent IOCTL values or install a filter speculatively.
- Trackpad standard pointer and two-finger scrolling should remain owned by a compatible Precision Touchpad driver. User-mode Raw Input code should only add custom gestures that have been tested not to break native input.
- Use the safe install/removal principles from the Trackpad wrapper: exact package identity, backups, no /force, connected-device removal guard, verification after actions, and an explicit recovery path.
- No proprietary Magic Utilities binaries or source are to be copied into the open-source app. They may be used only as lawful behavioral references. Third-party drivers retain their original licensing/signature.

## Acceptance gates (must all pass before calling it a working release)

### Build and packaging
- Clean Windows x64 build succeeds from a fresh checkout.
- Automated unit tests pass.
- Driver payload hashes and Authenticode signatures are verified where required.
- Installer runs from a path containing spaces.
- Install/upgrade/uninstall are idempotent and never touch unrelated Apple devices.

### Physical-device regression
Record exact Windows build, connection type, driver provider/version/INF, and result for each test.

| Test | Mouse | Trackpad | Keyboard |
|---|---|---|---|
| pointer | required | required | n/a |
| click / key input | required | required | required |
| scroll | vertical + horizontal | one/two finger | n/a |
| gestures | tap/swipe if implemented | native 2/3/4 finger where claimed | function row if implemented |
| battery | fresh percentage | fresh percentage | fresh percentage |
| sleep/wake | retain function | retain function | retain function |
| power off/on reconnect | retain function | retain function | retain function |
| missing/stale report | not 0% | not 0% | not 0% |

A release is not called complete until all three devices pass, battery readings stay independent of scrolling/input ownership, and rollback is verified. Until then, label it an integration/preview build and keep the currently working driver state recoverable.

## Current integration branch implementation

The branch now includes an experimental `magic_mouse_scroll.py` module and wiring in `pearipherals.py` / `pearipherals_core.py`:

- Reads the Magic Mouse 2015 HID touch collection in user mode using input-report reads only.
- Decodes report ID `0x12`, using 8-byte contact records after the 14-byte header for extended touch reports. The public protocol reference is the Linux kernel's `drivers/hid/hid-magicmouse.c`; this branch is an independent implementation and does not vendor that source.
- Requires exactly one firm contact (size at least 8) for the first scroll implementation; ambiguous multi-touch frames reset the scroll baseline rather than being guessed.
- Injects standard Windows vertical/horizontal wheel input, with smooth (12-unit deltas) and classic (120-unit detents) modes, natural-direction setting, and speed setting.
- Extends the existing HID battery poller to PID `0269` and protects report reads with a shared lock. A readable `0x90` response is still required for a real battery percentage.
- Includes pure unit tests for report decoding, signed coordinates, collection selection, natural direction, smooth/classic wheel deltas, weak contacts, and ambiguous multi-touch frames.

**Verification status:** source committed; no Windows-hosted CI result or physical-device result is claimed yet. The raw report layout is grounded in the public protocol reference, but this Windows implementation still needs automated test/build validation and physical confirmation that the expected collection can be opened and emits the reports. Keep it marked experimental until the acceptance gates pass.

Protocol reference: https://github.com/torvalds/linux/blob/master/drivers/hid/hid-magicmouse.c

## Immediate implementation sequence

1. Preserve the current default driver configuration and capture the exact current PnP state.
2. Create an application integration workspace; port/use existing tested Pearipherals components rather than blindly merging entire repositories.
3. Keep Mouse PID 0269 support separate from v3 PID 0323 research.
4. Establish a reliable Mouse 0269 touch-data source and confirm battery access on the same driver stack.
5. Integrate current Trackpad driver lifecycle and existing separate battery backends.
6. Run clean build + unit tests, then guided physical-device acceptance tests before any driver install or registry mutation.
