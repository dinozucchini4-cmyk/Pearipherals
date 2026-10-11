# Own Magic Utilities — repository integration map

This branch uses Pearipherals as the existing application base while evaluating a combined Apple peripheral app. It is a draft integration; it has **not yet been physically validated** with the user's Magic Mouse 2015 (PID 0269) and Magic Trackpad 2 (PID 0265) together.

## Which repository supplies which part

| Repository | Role in this integration | Important limit |
|---|---|---|
| [Pearipherals](https://github.com/OnlineFix/Pearipherals) / this fork | Existing tray app, keyboard function-row controls, three-finger Trackpad gestures, natural scrolling, battery poller and alert lifecycle | Current beta is unsigned; physical support is not claimed for every hardware revision |
| [magic-tray](https://github.com/dinozucchini4-cmyk/magic-tray) | Reference for Apple Mouse 2015 HID battery handling, HID descriptor inspection, phantom-interface rejection and safe driver guidance | Feature battery report must be selected from the live descriptor, not guessed |
| [MagicTrackpad2ForWindows](https://github.com/vitoplantamura/MagicTrackpad2ForWindows) | Preferred upstream Microsoft-signed Precision Touchpad driver option for Windows 11; Bluetooth, battery, haptic and control-panel support are documented upstream | The user's PID 0265 setup still requires physical verification |
| [magic-trackpad-windows](https://github.com/dinozucchini4-cmyk/magic-trackpad-windows) | Reference for safe installation, exact Driver Store identification and non-destructive uninstall patterns | This wrapper's validated hardware scope is narrower (newer A3120); this branch links directly to upstream v2.0 instead of claiming wrapper validation for PID 0265 |
| [mac-precision-touchpad](https://github.com/dinozucchini4-cmyk/mac-precision-touchpad) | Alternative legacy Precision Touchpad driver if upstream v2.0 does not work on this setup | Use it as an alternative, never stacked with another precision filter driver |
| [Apple-Input-Device-Drivers-Windows-10-11](https://github.com/dinozucchini4-cmyk/Apple-Input-Device-Drivers-Windows-10-11) | User-controlled setup guide for the Apple Wireless Mouse driver already shown to enable scrolling on this Magic Mouse 2015 | Driver files are not silently installed or bundled by the tray app |
| [magickeyboard](https://github.com/dinozucchini4-cmyk/magickeyboard) | Reference for future keyboard driver/layout work | The existing Windows keyboard/HID path should be preserved unless an actual keyboard issue requires change |
| [magic-mouse-v3-windows-fix](https://github.com/dinozucchini4-cmyk/magic-mouse-v3-windows-fix) | Compatibility research only | Targets Magic Mouse 2024 PID 0323, not this user's 2015 PID 0269; do not install it for the user's mouse |
| [MagicMouseWindows](https://github.com/dinozucchini4-cmyk/MagicMouseWindows) | Reference for scroll settings and tray UX only | Its README targets PID 0323 and requires the proprietary Magic Utilities driver, so it is not the final PID 0269 architecture |

The user's `paata` repository is currently empty and does not contribute code yet.

## Current implementation on this branch

### Magic Mouse 2015 — PID 0269

1. Poll the vendor HID input battery report `0x90` when the matching battery collection exposes it.
2. If that read fails, inspect matching HID collections' actual feature capabilities.
3. Query a feature report only if the descriptor declares Generic Device / Battery Strength (Usage Page `0x0006`, Usage `0x0020`). The feature report ID and length come from the descriptor; `0x47` is not hard-coded.
4. Reject malformed reports and phantom zero-percent readings.

Mouse scrolling still depends on the separately installed Apple Wireless Mouse driver for the tested PID 0269 setup. The tray's setup item opens the supplied driver repository; the app does not silently replace the driver stack.

### Magic Trackpad 2 — PID 0265

1. Poll the ordinary Apple HID battery collection first.
2. If a matching Trackpad 2 is enumerated but the ordinary HID battery read is unavailable, query the upstream driver's documented `\\.\AmtPtpControlDeviceUm` `IOCTL_GET_BATTERY` interface.
3. The fallback uses the control code defined in the upstream control-panel source (`CTL_CODE(FILE_DEVICE_UNKNOWN, 0x801, METHOD_BUFFERED, FILE_ANY_ACCESS)`, numeric value `0x222004`) and accepts only a successful 0–100 response.
4. Only the GET-battery IOCTL is called. The app does not call the driver's settings-reload IOCTL and does not write HID output/feature reports.

This path is useful only when a driver providing that control device is installed. If neither HID nor driver-level reading is available, the app reports unavailable rather than inventing a percentage.

## Drivers must not be stacked

Do not combine Magic Utilities' proprietary `MagicMouse.sys` with the Apple Wireless Mouse filter in `LowerFilters`. Earlier user tests found that this loses both scrolling and battery. This branch leaves driver selection to the user and does not edit `LowerFilters`.

For Trackpad 2, prefer the upstream v2.0 driver on Windows 11 if it works for PID 0265. Remove a previous precision driver before trying a different one. Do not install the `magic-trackpad-windows` wrapper or the legacy driver on top of the already active v2.0 driver.

## Validation still required

- Windows CI: full unit-test suite and Python byte-compilation gate.
- On the user's Windows 11 machine: verify Mouse scrolling and Mouse battery at the same time with the Apple Wireless Mouse driver active.
- On the same machine: verify Trackpad pointer, two-finger scroll, desired multi-finger gestures and battery at the same time with the selected Precision Touchpad driver active.
- Verify the tray reports unavailable when a report or driver interface does not respond; no false 0% readings.
- Preserve the original app/config and driver state until the combined behavior passes.

No proprietary Magic Utilities binaries are redistributed by this application branch.