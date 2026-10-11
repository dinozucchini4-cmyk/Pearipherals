"""Pearipherals — Apple's peripherals, minus the Apple computer.

Three parts, one tray app:

  Tragic Keyboard  Mac-style function row for the Magic Keyboard
  Tragic Trackpad  three-finger gestures the Bluetooth driver won't do
  Moodio Display   brightness for the Studio Display (and any other monitor)

F-row layout (Tragic Keyboard):

F1  brightness-  (Moodio: Studio Display USB / DDC-CI / gamma)
F2  brightness+
F3  Task View    (Win+Tab)   — Mission Control equivalent
F4  Search      (Win+S)      — Spotlight equivalent
F5  F5 (passthrough)
F6  Windows Snipping Tool selection overlay
F7  previous track
F8  play/pause
F9  next track
F10 mute
F11 volume down
F12 volume up

Holding ANY modifier (Ctrl/Alt/Shift/Win) passes F-keys through untouched,
so Alt+F4, Ctrl+F5, Shift+F10 keep working.

Tray icon: toggle the F-row, pick a gesture, autostart, quit.
Config: pearipherals.json next to this script.
"""
import ctypes
import ctypes.wintypes as w
import datetime
import json
import os
import platform
import queue
import sys
import threading
import time
import webbrowser

import pystray
from PIL import Image, ImageDraw

from pearipherals_core import (
    BatteryAlertPolicy,
    BatteryPoller,
    BatteryResult,
    BatterySnapshot,
    BatterySnapshotStore,
    CONFIG_LOCK,
    ConfigRecoveryRequired,
    HidBatteryBackend,
    query_amt_ptp_driver_battery,
    TouchpadSettingsManager,
    contacts_are_stable,
    expire_stale_contacts,
    format_battery_label,
    is_target_trackpad_path,
    load_json_config,
    read_optional_value,
    release_custom_input,
    require_single_input,
    run_input_callback,
    save_json_atomic,
    set_managed_mode,
    should_suppress_mouse_event,
    should_suppress_pointer,
    shutdown_custom_input,
)
from pearipherals_snipping import open_snipping_overlay
from pearipherals_support import (
    DIAGNOSTICS_FILENAME,
    SUPPORT_ENUMS,
    SupportSnapshot,
    battery_state,
    classify_autostart,
    classify_removal_readiness,
    diagnostics_payload,
    format_about,
    format_removal_summary,
    observe_version,
    perform_removal,
)
from pearipherals_version import (
    APP_VERSION,
    BuildIdentity,
    entry_source_build_id,
)

IS_FROZEN = getattr(sys, "frozen", False)
if IS_FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "pearipherals.json")
ERROR_LOG_PATH = os.path.join(APP_DIR, "pearipherals.err.log")
OLD_CONFIG_PATHS = (
    (os.path.join(APP_DIR, "magicsuite.json"), "MagicSuite"),
    (os.path.join(APP_DIR, "magickeys.json"), "MagicKeys"),
)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Pearipherals"
OLD_RUN_NAMES = ("MagicKeys", "MagicSuite")
# Frozen builds ship this deterministic manifest beside the executable.
BUILD_MANIFEST_NAME = "pearipherals-build.json"
# Used only when the exact identity cannot be determined; never a real digest.
UNKNOWN_BUILD_ID = "git:" + "0" * 40
# Fixed project links. Opening them requires an explicit user click; nothing
# here performs any network access on its own.
DOCS_URL = "https://github.com/OnlineFix/Pearipherals#readme"
REPORT_URL = "https://github.com/OnlineFix/Pearipherals/issues/new/choose"
MAGIC_EXTRA = 0xA99C0DE  # tag for our own injected events

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)

# Pointer-sized Win32 results must be declared explicitly on x64. ctypes
# otherwise defaults to c_int and can truncate valid HWND/HMODULE handles.
kernel32.GetModuleHandleW.argtypes = (w.LPCWSTR,)
kernel32.GetModuleHandleW.restype = w.HMODULE
kernel32.CreateMutexW.argtypes = (w.LPVOID, w.BOOL, w.LPCWSTR)
kernel32.CreateMutexW.restype = w.HANDLE
user32.CreateWindowExW.argtypes = (
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
)
user32.CreateWindowExW.restype = w.HWND
user32.PostMessageW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)
user32.PostMessageW.restype = w.BOOL
# Native dialogs are shown only from explicit tray callbacks. They are
# deliberately ownerless because pystray's two top-level HWNDs are invisible;
# using either as an owner prevents the visible MessageBox from activating.
# MessageBoxW still takes a pointer-sized optional HWND and returns int.
user32.MessageBoxW.argtypes = (w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT)
user32.MessageBoxW.restype = ctypes.c_int
MB_OK = 0x00000000
MB_ICONINFORMATION = 0x00000040
# Ownerless dialogs explicitly request foreground activation after the user
# selects the tray command.
MB_SETFOREGROUND = 0x00010000
# Destructive confirmation: warning icon, Yes/No/Cancel, and No pre-selected.
# Unlike MB_YESNO, the Cancel result also lets the title-bar X close safely.
MB_YESNOCANCEL = 0x00000003
MB_ICONWARNING = 0x00000030
MB_DEFBUTTON2 = 0x00000100
IDYES = 6
DIALOG_LOCK = threading.Lock()

# ---------------------------------------------------------------- config
DEFAULTS = {"mac_fkeys": True, "brightness_step": 10,
            "three_finger_mode": "swipes",   # swipes | drag | off
            "drag_gain": 0.75, "drag_grace_ms": 350, "drag_start_units": 30,
            "swipe_units": 300,
            "natural_scroll": False,   # False = swipe down scrolls down (wheel style)
            "tp_settings_applied": False,
            "current_version": None,
            "previous_version": None,
            "legacy_source": None,
            "cfg_version": 6}

CONFIG_RECOVERY_ERROR = None
_ERROR_LOG_LOCK = threading.Lock()


def append_error(message):
    """Best-effort thread-safe error logging for background Win32 callbacks."""
    try:
        with _ERROR_LOG_LOCK:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as stream:
                stream.write(
                    f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
                )
    except Exception:
        pass


def config_value(key, default=None):
    with CONFIG_LOCK:
        return config.get(key, default)


def load_config():
    # settings carry over from the pre-rename names (MagicSuite / MagicKeys)
    with CONFIG_LOCK:
        cfg, state = load_json_config(CONFIG_PATH, DEFAULTS)
        version = cfg.get("cfg_version", 1)
        if type(version) is not int or not 1 <= version <= 6:
            raise ConfigRecoveryRequired(
                CONFIG_PATH, ValueError("unsupported config schema version")
            )
        if state == "missing":
            for path, source_label in OLD_CONFIG_PATHS:
                candidate, old_state = load_json_config(path, DEFAULTS)
                if old_state == "loaded":
                    candidate_version = candidate.get("cfg_version", 1)
                    if (type(candidate_version) is not int
                            or not 1 <= candidate_version <= 6):
                        raise ConfigRecoveryRequired(
                            path, ValueError("unsupported config schema version")
                        )
                    candidate["legacy_source"] = source_label
                    # An adopted install already ran its first-run setup under
                    # the old name. Marking it onboarded stops first_run_setup
                    # from re-enabling autostart and rewriting touchpad
                    # registry values behind the user's back, unless the legacy
                    # config explicitly says setup never completed.
                    candidate["setup_done"] = bool(
                        candidate.get("setup_done", True)
                    )
                    save_config(candidate)  # atomically adopt before announcing it
                    cfg = candidate
                    break
        # v3: three_finger_drag bool replaced by three_finger_mode enum, and
        # swipe synthesis introduced (old driver can't feed native swipes).
        if cfg.get("cfg_version", 1) < 3:
            cfg.pop("three_finger_drag", None)
            cfg["three_finger_mode"] = "swipes"
        # v4 added an explicit scroll-direction preference.
        if cfg.get("cfg_version", 1) < 4:
            cfg["natural_scroll"] = False
        # v5 makes Apply/Restore state explicit. An old non-empty backup means
        # the app was still managing those values; an empty backup means they
        # had been restored (or no values ever needed changing).
        if cfg.get("cfg_version", 1) < 5:
            cfg["tp_settings_applied"] = bool(cfg.get("tp_settings_backup"))
            cfg["cfg_version"] = 5
            save_config(cfg)
        # v6 records only product lifecycle labels; no local source paths.
        if cfg.get("cfg_version", 1) < 6:
            cfg.setdefault("current_version", None)
            cfg.setdefault("previous_version", None)
            cfg.setdefault("legacy_source", None)
            cfg["cfg_version"] = 6
            save_config(cfg)
        return cfg


def save_config(cfg):
    with CONFIG_LOCK:
        if CONFIG_RECOVERY_ERROR is not None:
            raise CONFIG_RECOVERY_ERROR
        save_json_atomic(CONFIG_PATH, cfg)


try:
    config = load_config()
except ConfigRecoveryRequired as exc:
    CONFIG_RECOVERY_ERROR = exc
    config = {
        **DEFAULTS,
        "three_finger_mode": "off",
        "tp_settings_applied": False,
    }
    append_error(f"Config recovery required: {exc}")

LIFECYCLE_STATE = "not recorded"
if CONFIG_RECOVERY_ERROR is None:
    try:
        LIFECYCLE_STATE = observe_version(config, APP_VERSION, lambda: save_config(config))
    except Exception as exc:
        append_error(f"Version observation could not be persisted: {exc}")

# ---------------------------------------------------------------- SendInput
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTunion)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

user32.SendInput.argtypes = (w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = w.UINT


def send_keys(vks_down, vks_up=None):
    """Press vks_down in order, then release (reverse order by default)."""
    if vks_up is None:
        vks_up = list(reversed(vks_down))
    seq = [(vk, 0) for vk in vks_down] + [(vk, KEYEVENTF_KEYUP) for vk in vks_up]
    arr = (INPUT * len(seq))()
    for i, (vk, flags) in enumerate(seq):
        arr[i].type = INPUT_KEYBOARD
        arr[i].u.ki = KEYBDINPUT(vk, 0, flags, 0, MAGIC_EXTRA)
    user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))


VK_MEDIA_NEXT, VK_MEDIA_PREV, VK_MEDIA_PLAY = 0xB0, 0xB1, 0xB3
VK_VOL_MUTE, VK_VOL_DOWN, VK_VOL_UP = 0xAD, 0xAE, 0xAF
VK_LWIN, VK_TAB, VK_S = 0x5B, 0x09, 0x53
VK_ALT, VK_SHIFT, VK_D = 0x12, 0x10, 0x44
VK_M = 0x4D

# 3-finger swipe -> action (mac-like navigation on Windows)
# "keys": hotkey; "restore_all": un-minimize every window (undo swipe-down)
SWIPE_ACTIONS = {
    "up":    ("restore_all", None),
    "down":  ("minimize_all", None),          # snapshot, then Win+M
    "right": ("keys", ([VK_LWIN, VK_TAB],)),   # app choice (Task View)
    "left":  ("keys", ([VK_LWIN, VK_TAB],)),   # app choice (Task View)
}


_WINDOW_ENUM_PROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user32.EnumWindows.argtypes = [_WINDOW_ENUM_PROC, w.LPARAM]
user32.EnumWindows.restype = w.BOOL
for _name in ("IsWindow", "IsWindowVisible", "IsIconic", "SetForegroundWindow"):
    getattr(user32, _name).argtypes = [w.HWND]
    getattr(user32, _name).restype = w.BOOL
for _name in ("GetForegroundWindow", "GetShellWindow", "GetDesktopWindow"):
    getattr(user32, _name).argtypes = []
    getattr(user32, _name).restype = w.HWND
user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = w.LONG
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.SendMessageTimeoutW.argtypes = [
    w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT,
    ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, w.UINT]
user32.SetWindowPos.restype = w.BOOL

# Only the action worker owns this snapshot. Never infer pre-down order from
# enumeration after minimization, and never replace it on repeated down.
_window_cycle = None


def _window_identity(hwnd):
    pid = w.DWORD()
    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return (hwnd, pid.value, tid) if tid else None


def _cycle_windows():
    windows = []
    shell = (user32.GetShellWindow(), user32.GetDesktopWindow())

    @_WINDOW_ENUM_PROC
    def collect(hwnd, _):
        if (hwnd not in shell and user32.IsWindowVisible(hwnd)
                and not user32.GetWindowLongW(hwnd, -20) & 0x80):
            identity = _window_identity(hwnd)
            # The worker never attaches input queues. SendMessageTimeout's
            # timeout is ignored for its own queue, so exclude that thread.
            if identity and identity[2] != threading.get_native_id():
                windows.append(identity)
        return True

    if not user32.EnumWindows(collect, 0):
        # Never treat an incomplete enumeration as an empty/partial snapshot.
        raise RuntimeError("EnumWindows failed")
    return windows


def minimize_all_windows():
    global _window_cycle
    windows = _cycle_windows()
    if _window_cycle is None:
        _window_cycle = (windows, _window_identity(user32.GetForegroundWindow()))
    send_keys([VK_LWIN, VK_M])
    # SendInput only queues Win+M. Do not let an immediately queued Up race
    # Explorer's minimize operation. Uncooperative windows cannot hold the
    # worker indefinitely; this is a single shared one-second deadline.
    deadline = time.monotonic() + 1.0
    while any(_window_identity(h) == identity and not user32.IsIconic(h)
              for identity in windows for h in [identity[0]]):
        if time.monotonic() >= deadline:
            append_error("Window minimize: completion deadline expired")
            break
        time.sleep(0.01)


def _raise_cycle_window(identity):
    """Order without activation or geometry changes; bound cross-queue work."""
    hwnd = identity[0]
    band = user32.GetWindowLongW(hwnd, -20) & 0x8  # WS_EX_TOPMOST

    def at_top():
        same_band = [i for i in _cycle_windows()
                     if not user32.IsIconic(i[0])
                     and user32.GetWindowLongW(i[0], -20) & 0x8 == band]
        return bool(same_band and same_band[0] == identity)

    if at_top():
        return
    # HWND_TOP preserves the existing topmost/non-topmost band. Never use
    # HWND_TOPMOST/NOTOPMOST or attach input queues to force foreground.
    if not user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x4613):
        append_error("Window restore: Z-order request failed")
        return
    deadline = time.monotonic() + 0.25
    while _window_identity(hwnd) == identity and not at_top():
        if time.monotonic() >= deadline:
            append_error("Window restore: Z-order completion deadline expired")
            break
        time.sleep(0.01)


def restore_all_windows():
    """Restore all eligible minimized windows; finish before final activation."""
    global _window_cycle
    current = _cycle_windows()
    saved, foreground = _window_cycle if _window_cycle is not None else ([], None)

    # Previously minimized/new windows still belong in an Up, below the saved
    # stack. Validate PID/TID as well as HWND before using a saved identity.
    ordered = [identity for identity in saved if identity in current]
    ordered += [identity for identity in current if identity not in ordered
                and user32.IsIconic(identity[0])]
    for hwnd, pid, tid in reversed(ordered):
        if (_window_identity(hwnd) != (hwnd, pid, tid)
                or not user32.IsIconic(hwnd)):
            continue
        result = ctypes.c_size_t()
        # SC_RESTORE uses the window's own saved placement. Unlike
        # ShowWindowAsync, success here means the handler has returned, not
        # merely that an activating restore was queued on another input queue.
        if not user32.SendMessageTimeoutW(hwnd, 0x112, 0xF120, 0, 0x22, 250,
                                         ctypes.byref(result)):
            append_error("Window restore: command failed or timed out")
            continue
        # Custom handlers may defer their restore even after replying.
        deadline = time.monotonic() + 0.25
        while (_window_identity(hwnd) == (hwnd, pid, tid)
               and user32.IsIconic(hwnd) and time.monotonic() < deadline):
            time.sleep(0.01)
        if user32.IsIconic(hwnd):
            append_error("Window restore: completion deadline expired")
    for identity in reversed(ordered):
        if (_window_identity(identity[0]) == identity
                and not user32.IsIconic(identity[0])):
            _raise_cycle_window(identity)
    if (foreground in current and _window_identity(foreground[0]) == foreground
            and not user32.IsIconic(foreground[0])):
        if not user32.SetForegroundWindow(foreground[0]):
            append_error("Window restore: Windows denied foreground activation")
        else:
            deadline = time.monotonic() + 0.25
            while (_window_identity(foreground[0]) == foreground
                   and user32.GetForegroundWindow() != foreground[0]):
                if time.monotonic() >= deadline:
                    append_error("Window restore: foreground completion deadline expired")
                    break
                time.sleep(0.01)
    if not any(_window_identity(i[0]) == i and user32.IsIconic(i[0])
               for i in ordered):
        _window_cycle = None

# ---------------------------------------------------------------- DDC/CI brightness
class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", w.HANDLE),
                ("szPhysicalMonitorDescription", w.WCHAR * 128)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC,
                                     ctypes.POINTER(w.RECT), w.LPARAM)


class Brightness:
    """DDC/CI brightness for all physical monitors, with cached handles."""

    def __init__(self):
        self._mons = []          # list[(handle, minb, maxb)]
        self._lock = threading.Lock()

    def _close(self):
        for h, _, _ in self._mons:
            try:
                pm = PHYSICAL_MONITOR(h, "")
                dxva2.DestroyPhysicalMonitors(1, ctypes.byref(pm))
            except Exception:
                pass
        self._mons = []

    def _open(self):
        self._close()
        hmons = []

        def cb(hmon, hdc, rect, lparam):
            hmons.append(hmon)
            return True

        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
        for hmon in hmons:
            n = w.DWORD(0)
            if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(n)) or n.value == 0:
                continue
            arr = (PHYSICAL_MONITOR * n.value)()
            if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, n.value, arr):
                continue
            for pm in arr:
                mn, cur, mx = w.DWORD(), w.DWORD(), w.DWORD()
                if dxva2.GetMonitorBrightness(pm.hPhysicalMonitor, ctypes.byref(mn),
                                              ctypes.byref(cur), ctypes.byref(mx)):
                    self._mons.append((pm.hPhysicalMonitor, mn.value, mx.value))
                else:  # monitor without DDC support
                    one = PHYSICAL_MONITOR(pm.hPhysicalMonitor, "")
                    dxva2.DestroyPhysicalMonitors(1, ctypes.byref(one))

    def step(self, delta):
        """Returns new brightness % of first monitor, or None."""
        with self._lock:
            if not self._mons:
                self._open()
            result = None
            ok_any = False
            for h, mn, mx in self._mons:
                a, cur, b = w.DWORD(), w.DWORD(), w.DWORD()
                if not dxva2.GetMonitorBrightness(h, ctypes.byref(a), ctypes.byref(cur), ctypes.byref(b)):
                    continue
                span = max(1, mx - mn)
                pct = round((cur.value - mn) * 100 / span)
                pct = max(0, min(100, pct + delta))
                raw = mn + round(pct * span / 100)
                if dxva2.SetMonitorBrightness(h, raw):
                    ok_any = True
                    if result is None:
                        result = pct
            if not ok_any:      # stale handles (monitor sleep etc.) — reopen once
                self._open()
                for h, mn, mx in self._mons:
                    a, cur, b = w.DWORD(), w.DWORD(), w.DWORD()
                    if dxva2.GetMonitorBrightness(h, ctypes.byref(a), ctypes.byref(cur), ctypes.byref(b)):
                        span = max(1, mx - mn)
                        pct = round((cur.value - mn) * 100 / span)
                        pct = max(0, min(100, pct + delta))
                        if dxva2.SetMonitorBrightness(h, mn + round(pct * span / 100)):
                            if result is None:
                                result = pct
            return result


class StudioDisplayBrightness:
    """Apple Studio Display via USB HID feature report (needs USB link to display).

    Protocol from juliuszint/asdbctl: report id 1, u32 LE brightness 400..60000.
    """

    PIDS = (0x1114, 0x1116, 0x1118)
    MIN, MAX = 400, 60000

    def __init__(self):
        self._h = None
        self._lock = threading.Lock()

    def _open(self):
        import hid as hidapi
        self._h = None
        for pid in self.PIDS:
            for d in hidapi.enumerate(0x05AC, pid):
                try:
                    h = hidapi.device()
                    h.open_path(d["path"])
                    data = h.get_feature_report(1, 7)
                    if len(data) >= 5:
                        self._h = h
                        return True
                    h.close()
                except Exception:
                    pass
        return False

    def _pct(self, raw):
        return round((raw - self.MIN) * 100 / (self.MAX - self.MIN))

    def step(self, delta):
        with self._lock:
            for attempt in (0, 1):
                if self._h is None and not self._open():
                    return None
                try:
                    data = self._h.get_feature_report(1, 7)
                    raw = int.from_bytes(bytes(data[1:5]), "little")
                    pct = max(0, min(100, self._pct(raw) + delta))
                    nraw = self.MIN + round(pct * (self.MAX - self.MIN) / 100)
                    payload = bytes([1]) + nraw.to_bytes(4, "little") + b"\x00\x00"
                    if self._h.send_feature_report(payload) > 0:
                        return pct
                    raise OSError("send_feature_report failed")
                except Exception:
                    try:
                        self._h.close()
                    except Exception:
                        pass
                    self._h = None   # stale handle (unplug) — retry once
            return None


class GammaBrightness:
    """Fallback software dimming via SetDeviceGammaRamp (video-only links).

    Scales the GPU output curve on every attached display. Floor 20% so the
    screen can never go black.
    """

    def __init__(self):
        self.gdi32 = ctypes.WinDLL("gdi32")
        self.level = self._read_current_level()

    def _read_current_level(self):
        """Infer current dim level from the actual GPU gamma ramp (survives restarts)."""
        try:
            hdc = user32.GetDC(None)
            ramp = (w.WORD * (256 * 3))()
            if self.gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
                lvl = round(ramp[255] * 100 / 65535)
                user32.ReleaseDC(None, hdc)
                return max(20, min(100, lvl))
            user32.ReleaseDC(None, hdc)
        except Exception:
            pass
        return 100

    def _dcs(self):
        class DISPLAY_DEVICE(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("DeviceName", w.WCHAR * 32),
                        ("DeviceString", w.WCHAR * 128), ("StateFlags", w.DWORD),
                        ("DeviceID", w.WCHAR * 128), ("DeviceKey", w.WCHAR * 128)]
        i = 0
        dd = DISPLAY_DEVICE()
        dd.cb = ctypes.sizeof(dd)
        DISPLAY_DEVICE_ATTACHED = 0x01
        while user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            if dd.StateFlags & DISPLAY_DEVICE_ATTACHED:
                hdc = self.gdi32.CreateDCW(None, dd.DeviceName, None, None)
                if hdc:
                    yield hdc
            i += 1
            dd = DISPLAY_DEVICE()
            dd.cb = ctypes.sizeof(dd)

    def apply(self, level):
        level = max(20, min(100, level))
        ramp = (w.WORD * (256 * 3))()
        for i in range(256):
            v = min(65535, round(i * 257 * level / 100))
            ramp[i] = ramp[256 + i] = ramp[512 + i] = v
        ok = False
        for hdc in self._dcs():
            if self.gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
                ok = True
            self.gdi32.DeleteDC(hdc)
        if ok:
            self.level = level
            with CONFIG_LOCK:
                config["gamma_level"] = level
                save_config(config)
        return ok

    def step(self, delta):
        target = max(20, min(100, self.level + delta))
        return target if self.apply(target) else None

    def restore(self):
        self.apply(100)


class BrightnessChain:
    """Studio Display USB -> DDC/CI -> gamma dimming, first one that works."""

    def __init__(self):
        self.sd = StudioDisplayBrightness()
        self.ddc = Brightness()
        self.gamma = GammaBrightness()
        self.last_backend = None

    def step(self, delta):
        pct = self.sd.step(delta)
        if pct is not None:
            self.last_backend = "Studio Display"
            return pct
        pct = self.ddc.step(delta)
        if pct is not None:
            self.last_backend = "DDC"
            return pct
        pct = self.gamma.step(delta)
        self.last_backend = "software dim" if pct is not None else None
        return pct


brightness = BrightnessChain()

# ---------------------------------------------------------------- brightness OSD
class OSD:
    """Windows-11-style bottom-center brightness flyout (tkinter, own thread)."""

    def __init__(self):
        self._q = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def show(self, pct, backend):
        self._q.put((pct, backend))

    def _run(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.94)
        root.configure(bg="#202020")

        W, H = 280, 64
        cv = tk.Canvas(root, width=W, height=H, bg="#202020",
                       highlightthickness=0, bd=0)
        cv.pack()

        sun = cv.create_text(30, H // 2, text="\u2600", font=("Segoe UI Emoji", 18),
                             fill="#ffffff")
        BAR_X, BAR_W, BAR_H = 58, 160, 6
        bar_y = H // 2
        cv.create_rectangle(BAR_X, bar_y - BAR_H // 2, BAR_X + BAR_W,
                            bar_y + BAR_H // 2, fill="#5a5a5a", width=0)
        fill = cv.create_rectangle(BAR_X, bar_y - BAR_H // 2, BAR_X,
                                   bar_y + BAR_H // 2, fill="#ffffff", width=0)
        label = cv.create_text(W - 30, H // 2, text="", font=("Segoe UI", 11),
                               fill="#ffffff")
        hide_job = [None]

        def place():
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            root.geometry(f"{W}x{H}+{(sw - W) // 2}+{sh - H - 76}")

        def tick():
            try:
                while True:
                    pct, backend = self._q.get_nowait()
                    cv.coords(fill, BAR_X, bar_y - BAR_H // 2,
                              BAR_X + round(BAR_W * pct / 100), bar_y + BAR_H // 2)
                    cv.itemconfigure(label, text=f"{pct}%")
                    cv.itemconfigure(sun, text="\u263c" if backend == "software dim" else "\u2600")
                    place()
                    root.deiconify()
                    root.lift()
                    if hide_job[0]:
                        root.after_cancel(hide_job[0])
                    hide_job[0] = root.after(1400, root.withdraw)
            except queue.Empty:
                pass
            root.after(30, tick)

        tick()
        root.mainloop()


osd = OSD()

# ---------------------------------------------------------------- action worker
actions = queue.Queue()
tray_icon = None  # set later


def worker():
    pending = None
    while True:
        item = pending if pending is not None else actions.get()
        pending = None
        if item is None:
            return
        kind, arg = item
        try:
            if kind == "keys":
                send_keys(*arg)
            elif kind == "minimize_all":
                minimize_all_windows()
            elif kind == "restore_all":
                restore_all_windows()
            elif kind == "snip":
                open_snipping_overlay()
            elif kind == "bright":
                pct = brightness.step(arg)
                if pct is not None:
                    osd.show(pct, brightness.last_backend)
                    if tray_icon is not None:
                        tray_icon.title = (f"Moodio — brightness {pct}% "
                                           f"({brightness.last_backend})")
        except Exception as exc:
            if kind in ("minimize_all", "restore_all"):
                append_error(f"Window {kind.removesuffix('_all')} failed: {exc}")
            elif kind == "snip":
                append_error(f"Snipping overlay launch failed: {exc}")
                _notify(
                    tray_icon,
                    "Windows' snipping overlay could not be opened.",
                    "Snipping Tool failed",
                )
        # Coalesce queued repeats of the same action when a key is held down.
        try:
            while True:
                nxt = actions.get_nowait()
                if nxt != item:
                    if nxt is None:
                        return
                    pending = nxt
                    break
        except queue.Empty:
            pass


# ---------------------------------------------------------------- LL keyboard hook
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_KEYUP, WM_SYSKEYUP = 0x0101, 0x0105
LLKHF_INJECTED = 0x10
VK_F1, VK_F12 = 0x70, 0x7B
VK_F6 = 0x75
MOD_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # shift, ctrl, alt, lwin, rwin
SNIP_KEY_STATE = "up"


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, w.WPARAM, ctypes.c_longlong)

user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, w.HINSTANCE, w.DWORD)
user32.SetWindowsHookExW.restype = w.HHOOK
user32.CallNextHookEx.argtypes = (w.HHOOK, ctypes.c_int, w.WPARAM, ctypes.c_longlong)
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.UnhookWindowsHookEx.argtypes = (w.HHOOK,)
user32.UnhookWindowsHookEx.restype = w.BOOL
user32.PostThreadMessageW.argtypes = (w.DWORD, w.UINT, w.WPARAM, w.LPARAM)
user32.PostThreadMessageW.restype = w.BOOL
kernel32.GetCurrentThreadId.restype = w.DWORD

# F-key -> action table
ACTIONS = {
    0x70: ("bright", None),            # F1 (delta filled at dispatch)
    0x71: ("bright", None),            # F2
    0x72: ("keys", ([VK_LWIN, VK_TAB],)),   # F3  Task View
    0x73: ("keys", ([VK_LWIN, VK_S],)),     # F4  Search
    0x75: ("snip", None),                  # F6  Windows snipping overlay
    0x76: ("keys", ([VK_MEDIA_PREV],)),     # F7
    0x77: ("keys", ([VK_MEDIA_PLAY],)),     # F8
    0x78: ("keys", ([VK_MEDIA_NEXT],)),     # F9
    0x79: ("keys", ([VK_VOL_MUTE],)),       # F10
    0x7A: ("keys", ([VK_VOL_DOWN],)),       # F11
    0x7B: ("keys", ([VK_VOL_UP],)),         # F12
}


@HOOKPROC
def ll_hook(nCode, wParam, lParam):
    global SNIP_KEY_STATE
    if nCode == 0:
        kb = ctypes.cast(ctypes.c_void_p(lParam), ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode
        physical = (
            not (kb.flags & LLKHF_INJECTED)
            and kb.dwExtraInfo != MAGIC_EXTRA
        )

        # F6 has per-press ownership. Once a physical press begins as either the
        # snipping overlay or passthrough, modifier/config changes cannot switch it
        # mid-press. Key-up is handled even while the F-row is disabled.
        if vk == VK_F6 and physical:
            if wParam in (WM_KEYUP, WM_SYSKEYUP):
                previous = SNIP_KEY_STATE
                SNIP_KEY_STATE = "up"
                if previous == "captured":
                    return 1
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if SNIP_KEY_STATE == "captured":
                    return 1
                if SNIP_KEY_STATE == "passthrough":
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                if (
                    not config_value("mac_fkeys")
                    or any(user32.GetAsyncKeyState(m) & 0x8000 for m in MOD_VKS)
                ):
                    SNIP_KEY_STATE = "passthrough"
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                SNIP_KEY_STATE = "captured"
                actions.put(("snip", None))
                return 1

        if config_value("mac_fkeys"):
            if VK_F1 <= vk <= VK_F12 and vk in ACTIONS and physical:
                if any(user32.GetAsyncKeyState(m) & 0x8000 for m in MOD_VKS):
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    kind, arg = ACTIONS[vk]
                    if kind == "bright":
                        step = config_value("brightness_step")
                        delta = -step if vk == 0x70 else step
                        actions.put(("bright", delta))
                    else:
                        actions.put((kind, arg))
                return 1  # swallow both keydown and keyup
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


def hook_thread():
    hk = user32.SetWindowsHookExW(WH_KEYBOARD_LL, ll_hook, None, 0)
    if not hk:
        sys.exit(f"SetWindowsHookExW failed: {ctypes.get_last_error()}")
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# ---------------------------------------------------------------- pointer suppressor
class PointerSuppressor:
    """Install the global mouse hook only during a confirmed 3-finger touch.

    A Python WH_MOUSE_LL callback is synchronous with system pointer delivery.
    Leaving it installed while idle made every ordinary mouse move cross the
    Python process and could add jitter even when gesture mode was off.
    """

    WH_MOUSE_LL = 14
    WM_MOUSEMOVE = 0x0200
    WM_MOUSEWHEEL = 0x020A
    WM_MOUSEHWHEEL = 0x020E
    WM_QUIT = 0x0012
    WM_INSTALL = 0x8001
    WM_UNINSTALL = 0x8002
    LLMHF_INJECTED = 0x01

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", w.POINT), ("mouseData", w.DWORD), ("flags", w.DWORD),
                    ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]

    def __init__(self):
        self._active = False
        self._requested_active = False
        self._proc = None
        self._hook = None
        self._thread = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._operation_done = threading.Event()
        self._operation_error = None
        self._operation_generation = 0
        self._request_generation = 0
        self._request_deadline = None
        self._lock = threading.Lock()

    def set(self, active):
        active = bool(active)
        if active:
            self.start()
            self._request_hook_change(True)
            return

        with self._lock:
            self._requested_active = False
            self._request_generation += 1
            if self._hook is None:
                self._active = False
                return
            thread_available = (
                self._thread is not None
                and self._thread.is_alive()
                and self._thread_id
            )
        if not thread_available:
            for _ in range(3):
                if self._uninstall_hook():
                    return
            raise OSError("UnhookWindowsHookEx failed after bounded retries")
        self._request_hook_change(False)

    def set_contacts(self, n):
        # Kept for diagnostics; hook activation is based on qualified contacts.
        self._ncontacts = n

    def start(self):
        with self._lock:
            if (
                self._thread is not None
                and self._thread.is_alive()
                and self._thread_id
            ):
                return
            if self._thread is not None and self._thread.is_alive():
                # A prior readiness wait timed out. Re-wait the same thread;
                # spawning a duplicate could leave two message loops/hooks.
                thread = self._thread
            else:
                self._thread = None
                self._thread_id = 0
                self._ready.clear()
                thread = threading.Thread(target=self._run, daemon=True)
                self._thread = thread
                thread.start()
        ready = self._ready.wait(1.0)
        with self._lock:
            alive = thread.is_alive()
            if not ready or not alive or not self._thread_id:
                self._requested_active = False
                if self._hook is None:
                    self._active = False
                if not alive and self._thread is thread:
                    self._thread = None
                raise OSError("pointer suppressor hook thread did not become ready")

    def _install_hook(self, request_generation=None):
        with self._lock:
            if request_generation is None:
                request_generation = self._request_generation
        hook = user32.SetWindowsHookExW(
            self.WH_MOUSE_LL, self._proc, None, 0
        )
        with self._lock:
            if not hook:
                if self._request_generation == request_generation:
                    self._requested_active = False
                    self._active = False
                return False
            still_requested = (
                self._request_generation == request_generation
                and self._requested_active
                and self._hook is None
                and (
                    self._request_deadline is None
                    or time.monotonic() <= self._request_deadline
                )
            )
            if still_requested:
                self._hook = hook
                self._active = True
                return True

        # SetWindowsHookExW may finish after its caller timed out or a newer
        # enable/disable request took ownership. Never publish that stale hook
        # active; remove it immediately. If removal fails, retain the handle so
        # bounded disable/shutdown retries can still remove it later.
        if not user32.UnhookWindowsHookEx(hook):
            with self._lock:
                if self._hook is None:
                    self._hook = hook
                    self._active = False
        return False

    def _uninstall_hook(self):
        with self._lock:
            hook = self._hook
        if hook is None:
            with self._lock:
                self._active = False
            return True
        if not user32.UnhookWindowsHookEx(hook):
            # A failed unhook is still installed. Retain the handle and active
            # state so a later bounded disable/shutdown attempt can retry it.
            return False
        with self._lock:
            if self._hook == hook:
                self._hook = None
                self._active = False
        return True

    def _request_hook_change(self, active):
        attempts = 1 if active else 3
        last_error = None
        with self._lock:
            thread = self._thread
            thread_id = self._thread_id
            if not thread_id or thread is None or not thread.is_alive():
                if active and self._hook is None:
                    self._active = False
                raise OSError("pointer suppressor hook thread is unavailable")
            self._request_generation += 1
            request_generation = self._request_generation
            self._requested_active = active
        for _ in range(attempts):
            with self._lock:
                self._operation_error = None
                self._operation_done.clear()
                deadline = time.monotonic() + 1.0
                self._request_deadline = deadline if active else None
            if not user32.PostThreadMessageW(
                thread_id,
                self.WM_INSTALL if active else self.WM_UNINSTALL,
                request_generation,
                0,
            ):
                last_error = OSError(
                    ctypes.get_last_error(), "PostThreadMessageW failed"
                )
                with self._lock:
                    if active and self._request_generation == request_generation:
                        self._request_generation += 1
                        self._requested_active = False
                        if self._hook is None:
                            self._active = False
                break
            while True:
                completed = self._operation_done.wait(
                    max(0.0, deadline - time.monotonic())
                )
                with self._lock:
                    completion_owned = (
                        self._operation_generation == request_generation
                    )
                    superseded = self._request_generation != request_generation
                if not completed or completion_owned or superseded:
                    break
                # A stale operation completed after this request cleared the
                # shared event. Ignore that wakeup without extending the bound.
                self._operation_done.clear()
            with self._lock:
                operation_error = (
                    self._operation_error if completion_owned else None
                )
                state_matches = (
                    self._hook is not None and self._active
                    if active else self._hook is None
                )
                thread_alive = self._thread is thread and thread.is_alive()
            if (completed and completion_owned and operation_error is None
                    and state_matches):
                return
            if operation_error is not None:
                last_error = operation_error
            elif not completed:
                last_error = OSError("pointer suppressor hook operation timed out")
            elif superseded:
                last_error = OSError("pointer suppressor hook request was superseded")
            elif not thread_alive:
                last_error = OSError("pointer suppressor hook thread exited early")
            else:
                last_error = OSError("pointer suppressor hook operation failed")
            if active:
                break
        cleanup_late_install = False
        with self._lock:
            if active and self._request_generation == request_generation:
                self._request_generation += 1
                self._requested_active = False
                if self._hook is None:
                    self._active = False
                else:
                    cleanup_late_install = True
        if cleanup_late_install:
            try:
                self._request_hook_change(False)
            except Exception:
                # Failed unhook state must remain visible and retryable.
                pass
        raise last_error

    def _run(self):
        HOOKPROC_M = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int,
                                        w.WPARAM, ctypes.c_longlong)

        def proc(nCode, wParam, lParam):
            if nCode == 0 and self._active:
                ms = ctypes.cast(ctypes.c_void_p(lParam),
                                 ctypes.POINTER(self.MSLLHOOKSTRUCT)).contents
                if should_suppress_mouse_event(
                    wParam, ms.flags, ms.dwExtraInfo, self._active,
                    self.LLMHF_INJECTED, MAGIC_EXTRA,
                ):
                    return 1
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc = HOOKPROC_M(proc)
        thread_id = kernel32.GetCurrentThreadId()
        msg = w.MSG()
        # PeekMessage creates this thread's message queue before callers post.
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        with self._lock:
            self._thread_id = thread_id
        self._ready.set()

        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_INSTALL:
                    request_generation = int(msg.wParam)
                    with self._lock:
                        owns_request = (
                            self._requested_active
                            and self._request_generation == request_generation
                            and (
                                self._request_deadline is None
                                or time.monotonic() <= self._request_deadline
                            )
                        )
                        should_install = (
                            self._hook is None
                            and owns_request
                        )
                        if self._hook is not None and owns_request:
                            # A cancelled late install whose immediate unhook
                            # failed can be safely reused by a newer activation.
                            self._active = True
                    installed = (
                        self._install_hook(request_generation)
                        if should_install else False
                    )
                    with self._lock:
                        self._operation_error = None
                        if (should_install and not installed
                                and self._request_generation == request_generation):
                            self._operation_error = OSError(
                                ctypes.get_last_error(),
                                "SetWindowsHookExW failed",
                            )
                        self._operation_generation = request_generation
                    self._operation_done.set()
                elif msg.message == self.WM_UNINSTALL:
                    request_generation = int(msg.wParam)
                    uninstalled = self._uninstall_hook()
                    with self._lock:
                        self._operation_error = None
                        if not uninstalled:
                            self._operation_error = OSError(
                                ctypes.get_last_error(),
                                "UnhookWindowsHookEx failed",
                            )
                        self._operation_generation = request_generation
                    self._operation_done.set()
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._uninstall_hook()
            with self._lock:
                self._requested_active = False
                self._thread_id = 0
                if self._hook is None:
                    self._active = False
            self._operation_done.set()

    def shutdown(self):
        try:
            self.set(False)
        except Exception:
            # Preserve the hook state for the final thread-exit unhook attempt.
            pass
        with self._lock:
            thread_id = self._thread_id
            thread = self._thread
        if thread_id:
            user32.PostThreadMessageW(thread_id, self.WM_QUIT, 0, 0)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            if thread is not None and not thread.is_alive() and self._thread is thread:
                self._thread = None


suppress_pointer = PointerSuppressor()

# ---------------------------------------------------------------- 3-finger drag
class ThreeFingerDrag:
    """macOS-style three-finger drag via Raw Input PTP frames.

    Reads Precision Touchpad contact frames (usage 0x0D/0x05) in parallel with
    the OS, and when exactly 3 confident contacts move, holds the left mouse
    button and moves the cursor itself. Grace period lets you lift/reposition
    fingers mid-drag like on a Mac.
    """

    WM_INPUT = 0x00FF
    WM_INPUT_DEVICE_CHANGE = 0x00FE
    WM_TIMER = 0x0113
    TIMER_ID = 1
    RIDEV_INPUTSINK = 0x00000100
    RIDEV_DEVNOTIFY = 0x00002000
    GIDC_REMOVAL = 2
    RIM_TYPEHID = 2
    RID_INPUT = 0x10000003
    RIDI_PREPARSEDDATA = 0x20000005
    RIDI_DEVICENAME = 0x20000007
    HIDP_SUCCESS = 0x00110000
    UP_DIG, U_TOUCHPAD = 0x0D, 0x05
    U_X, U_Y, U_CID, U_COUNT, U_TIP, U_CONF = 0x30, 0x31, 0x51, 0x54, 0x42, 0x47

    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004

    class VALUE_CAPS(ctypes.Structure):
        _fields_ = [("UsagePage", w.WORD), ("ReportID", ctypes.c_ubyte),
                    ("IsAlias", ctypes.c_ubyte), ("BitField", w.WORD),
                    ("LinkCollection", w.WORD), ("LinkUsage", w.WORD),
                    ("LinkUsagePage", w.WORD), ("IsRange", ctypes.c_ubyte),
                    ("IsStringRange", ctypes.c_ubyte), ("IsDesignatorRange", ctypes.c_ubyte),
                    ("IsAbsolute", ctypes.c_ubyte), ("HasNull", ctypes.c_ubyte),
                    ("Reserved", ctypes.c_ubyte), ("BitSize", w.WORD),
                    ("ReportCount", w.WORD), ("Reserved2", w.WORD * 5),
                    ("UnitsExp", w.DWORD), ("Units", w.DWORD),
                    ("LogicalMin", ctypes.c_long), ("LogicalMax", ctypes.c_long),
                    ("PhysicalMin", ctypes.c_long), ("PhysicalMax", ctypes.c_long),
                    ("UsageMin", w.WORD), ("UsageMax", w.WORD),
                    ("StringMin", w.WORD), ("StringMax", w.WORD),
                    ("DesignatorMin", w.WORD), ("DesignatorMax", w.WORD),
                    ("DataIndexMin", w.WORD), ("DataIndexMax", w.WORD)]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", w.WORD), ("usUsage", w.WORD),
                    ("dwFlags", w.DWORD), ("hwndTarget", w.HWND)]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [("dwType", w.DWORD), ("dwSize", w.DWORD),
                    ("hDevice", w.HANDLE), ("wParam", w.WPARAM)]

    def __init__(self):
        self.hid = ctypes.WinDLL("hid")
        self.hid.HidP_GetUsageValue.argtypes = [
            ctypes.c_int, w.WORD, w.WORD, w.WORD, ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong]
        self.hid.HidP_GetUsages.argtypes = [
            ctypes.c_int, w.WORD, w.WORD, ctypes.POINTER(w.WORD),
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
            ctypes.c_char_p, ctypes.c_ulong]
        self.hid.HidP_GetValueCaps.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(w.WORD), ctypes.c_void_p]
        user32.GetRawInputData.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint), ctypes.c_uint]
        user32.GetRawInputDeviceInfoW.argtypes = [
            w.HANDLE, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        user32.RegisterRawInputDevices.argtypes = [
            ctypes.POINTER(self.RAWINPUTDEVICE), ctypes.c_uint, ctypes.c_uint]
        user32.RegisterRawInputDevices.restype = w.BOOL
        user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT,
                                    ctypes.c_void_p]
        user32.SetTimer.restype = ctypes.c_size_t

        self._pp = {}            # hDevice -> preparsed buffer
        self._links = {}         # hDevice -> [contact link collections]
        self._is_tp = {}         # hDevice -> bool (trackpad device filter)
        # Hybrid-report assembly: the AmtPtp driver sends ONE contact per HID
        # report; a "frame" only exists as a rolling window. Track contacts by
        # ID and expire the ones that stop reporting.
        self._touch = {}         # (hDevice, cid) -> [x, y, last_ts, x0, y0, t0]
        self.TOUCH_TTL = 0.07    # s without an update -> contact considered gone
        self.FRESH = 0.06        # every contact must be this fresh to fire
        self.MIN_AGE = 0.06      # and this old — filters ghost IDs from churn
        self.MAX_CONTACT_JUMP = 1500
        self.ALL_UP_DEBOUNCE = 0.05
        # drag state machine: idle -> armed -> dragging (-> grace -> dragging)
        self._state = "idle"
        self._anchor = None      # (x, y) three-finger centroid at arm time
        self._last = None        # last centroid
        self._grace_deadline = 0.0
        self._residual = [0.0, 0.0]
        self._swipe_fired = False
        self._all_up_since = None
        self._wndproc_ref = None
        self._thread = None
        # Conservative registration observability. This records only what this
        # process did (registered a listener or failed to), never whether a
        # target device exists, is awake, or is bound to anything.
        self._raw_input_state = "not started"
        self._raw_input_lock = threading.Lock()

    # Single source of truth: the support model owns the allowed wording, so a
    # new state can never be accepted here and rejected by SupportSnapshot.
    RAW_INPUT_STATES = tuple(sorted(SUPPORT_ENUMS["raw_input_state"]))

    def _note_raw_input_state(self, state):
        """Record one fixed registration state; reject over-claiming wording."""
        if state not in self.RAW_INPUT_STATES:
            raise ValueError(f"unsupported raw input registration state: {state}")
        with self._raw_input_lock:
            self._raw_input_state = state

    def raw_input_state(self):
        """Return the fixed registration state; never a handle or device path."""
        with self._raw_input_lock:
            return self._raw_input_state

    # ---------------- device/report helpers ----------------
    def _device_ok(self, hdev):
        ok = self._is_tp.get(hdev)
        if ok is None:
            size = ctypes.c_uint(0)
            user32.GetRawInputDeviceInfoW(hdev, self.RIDI_DEVICENAME, None,
                                          ctypes.byref(size))
            buf = ctypes.create_unicode_buffer(size.value + 2)
            user32.GetRawInputDeviceInfoW(hdev, self.RIDI_DEVICENAME, buf,
                                          ctypes.byref(size))
            ok = is_target_trackpad_path(buf.value)
            self._is_tp[hdev] = ok
        return ok

    def _preparsed(self, hdev):
        if hdev in self._pp:
            return self._pp[hdev]
        size = ctypes.c_uint(0)
        user32.GetRawInputDeviceInfoW(hdev, self.RIDI_PREPARSEDDATA, None,
                                      ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value) if size.value else None
        if buf:
            user32.GetRawInputDeviceInfoW(hdev, self.RIDI_PREPARSEDDATA, buf,
                                          ctypes.byref(size))
        self._pp[hdev] = buf
        links = []
        if buf:
            n = w.WORD(128)
            vcaps = (self.VALUE_CAPS * 128)()
            if self.hid.HidP_GetValueCaps(0, vcaps, ctypes.byref(n),
                                          buf) == self.HIDP_SUCCESS:
                for i in range(n.value):
                    c = vcaps[i]
                    if c.UsagePage == 0x01 and c.UsageMin == self.U_X:
                        links.append(c.LinkCollection)
        self._links[hdev] = sorted(set(links))
        return buf

    def _contacts(self, hdev, rep):
        """Yield (contact_id, x, y, tip) for every contact in this report."""
        pp = self._preparsed(hdev)
        if not pp:
            return []
        val = ctypes.c_ulong(0)
        out = []
        for lc in self._links[hdev]:
            if self.hid.HidP_GetUsageValue(0, 0x01, lc, self.U_X, ctypes.byref(val),
                                           pp, rep, len(rep)) != self.HIDP_SUCCESS:
                continue
            x = val.value
            if self.hid.HidP_GetUsageValue(0, 0x01, lc, self.U_Y, ctypes.byref(val),
                                           pp, rep, len(rep)) != self.HIDP_SUCCESS:
                continue
            y = val.value
            cid = lc  # fallback: link collection index
            if self.hid.HidP_GetUsageValue(0, self.UP_DIG, lc, self.U_CID,
                                           ctypes.byref(val), pp, rep,
                                           len(rep)) == self.HIDP_SUCCESS:
                cid = val.value
            n = ctypes.c_ulong(8)
            ub = (w.WORD * 8)()
            tip = conf = False
            if self.hid.HidP_GetUsages(0, self.UP_DIG, lc, ub, ctypes.byref(n),
                                       pp, rep, len(rep)) == self.HIDP_SUCCESS:
                got = {ub[i] for i in range(n.value)}
                tip, conf = self.U_TIP in got, self.U_CONF in got
            # Emit every readable slot, including Tip=0/Confidence=0. The
            # all-clear report is an explicit lift for a known CID; dropping it
            # would leave suppression active until TTL expiry.
            out.append((cid, x, y, tip and conf))
        return out

    # ---------------- mouse synthesis ----------------
    def _mouse(self, flags, dx=0, dy=0):
        inp = INPUT()
        inp.type = 0  # INPUT_MOUSE
        inp.u.mi = MOUSEINPUT(dx, dy, 0, flags, 0, MAGIC_EXTRA)
        require_single_input(
            lambda: user32.SendInput(
                1, ctypes.byref(inp), ctypes.sizeof(INPUT)
            )
        )

    def _button(self, down):
        self._mouse(self.MOUSEEVENTF_LEFTDOWN if down else self.MOUSEEVENTF_LEFTUP)

    def _move(self, fdx, fdy):
        self._residual[0] += fdx
        self._residual[1] += fdy
        dx, dy = int(self._residual[0]), int(self._residual[1])
        if dx or dy:
            self._residual[0] -= dx
            self._residual[1] -= dy
            self._mouse(self.MOUSEEVENTF_MOVE, dx, dy)

    # ---------------- state machine ----------------
    def _end_drag(self):
        self._button(False)
        self._state = "idle"
        self._anchor = self._last = None

    def _frame(self, hdev, rep):
        # Share the reentrant config lock: no input/config lock-order inversion.
        with CONFIG_LOCK:
            if self._input_stopped:
                return
            self._frame_locked(hdev, rep)

    def _frame_locked(self, hdev, rep):
        now = time.monotonic()

        # Merge this report's contacts into the rolling touch table while
        # collecting per-contact motion deltas.
        moved = []
        for cid, x, y, tip in self._contacts(hdev, rep):
            if cid == 0xFFFF or x > 30000 or y > 30000:
                continue          # garbage/padding contact
            key = (hdev, cid)
            if tip:
                e = self._touch.get(key)
                if e is not None:
                    dx, dy = x - e[0], y - e[1]
                    reused = (
                        now - e[2] > self.FRESH
                        or dx * dx + dy * dy > self.MAX_CONTACT_JUMP ** 2
                    )
                    if reused:
                        # The driver can recycle a CID without an intervening
                        # lift. Re-anchor it without producing cursor motion.
                        self._touch[key] = [x, y, now, x, y, now]
                    else:
                        moved.append((dx, dy))
                        e[0], e[1], e[2] = x, y, now
                else:
                    self._touch[key] = [x, y, now, x, y, now]
            else:
                self._touch.pop(key, None)   # explicit lift
        self._process_state(now, moved)

    def _process_state(self, now, moved):
        """Expire silent contacts, update suppression, and advance gestures."""
        with CONFIG_LOCK:
            if self._input_stopped:
                return
            self._process_state_locked(now, moved)

    def _process_state_locked(self, now, moved):
        expire_stale_contacts(self._touch, now, self.TOUCH_TTL)
        n = len(self._touch)
        suppress_pointer.set_contacts(n)
        mode = config_value("three_finger_mode")

        if mode != "drag" and self._state in ("dragging", "grace"):
            # A failed transition LEFTUP must retry even in swipes mode. Do not
            # synthesize anything in the new mode until the old button is up.
            self.abort_gesture()
            return

        if mode == "off":
            if self._state in ("dragging", "grace"):
                self._end_drag()
            self._state = "idle"
            self._touch.clear()
            self._swipe_fired = False
            self._all_up_since = None
            suppress_pointer.set(False)
            return

        # Freeze leaked pointer motion only after all three contacts pass the
        # same freshness/maturity gates as gesture recognition. Contact-ID
        # churn must never freeze ordinary one-finger movement.
        suppress_pointer.set(should_suppress_pointer(
            self._touch, now, self.FRESH, self.MIN_AGE
        ))

        if mode == "drag":
            self._frame_drag(n, now, moved)
        else:
            self._frame_swipes(n, now)

    def _quality(self, entries, now):
        """All contacts fresh (still updating) and aged (not churn ghosts)."""
        return contacts_are_stable(entries, now, self.FRESH, self.MIN_AGE)

    def _frame_drag(self, n, now, moved):
        # Losing quality is equivalent to losing a finger for an owned drag:
        # pause motion, retain the original grace deadline, and resume only
        # after the full set is fresh and mature again.
        if n == 3 and (self._state not in ("dragging", "grace")
                       or self._quality(list(self._touch.values()), now)):
            entries = list(self._touch.values())
            if self._state == "idle":
                for e in entries:            # snapshot per-contact anchors
                    e[3], e[4] = e[0], e[1]
                self._state = "armed"
            elif self._state == "armed":
                mdx = sum(e[0] - e[3] for e in entries) / 3.0
                mdy = sum(e[1] - e[4] for e in entries) / 3.0
                if (mdx * mdx + mdy * mdy) ** 0.5 >= config_value("drag_start_units") \
                        and self._quality(entries, now):
                    self._button(True)
                    self._state = "dragging"
                    self._residual = [0.0, 0.0]
            elif self._state == "grace":
                self._state = "dragging"     # fingers back — same drag
            elif self._state == "dragging" and moved:
                gain = config_value("drag_gain")
                dx = sum(m[0] for m in moved)
                dy = sum(m[1] for m in moved)
                self._move(dx * gain, dy * gain)
        else:
            if self._state == "dragging":
                self._state = "grace"
                self._grace_deadline = now + config_value("drag_grace_ms") / 1000.0
            elif self._state == "grace":
                if now > self._grace_deadline:
                    self._end_drag()
            elif self._state == "armed" and n == 0:
                self._state = "idle"

    def _frame_swipes(self, n, now):
        """One swipe per debounced all-up-delimited touchdown epoch."""
        if n == 0:
            if self._all_up_since is None:
                self._all_up_since = now
            elif now - self._all_up_since >= self.ALL_UP_DEBOUNCE:
                self._swipe_fired = False
                if self._state in ("armed", "fired"):
                    self._state = "idle"
                    self._anchor = self._last = None
            return

        self._all_up_since = None
        if self._swipe_fired:
            return

        if n == 3:
            entries = list(self._touch.values())
            if self._state == "idle":
                for e in entries:            # snapshot per-contact anchors
                    e[3], e[4] = e[0], e[1]
                self._state = "armed"
            elif self._state == "armed" and self._quality(entries, now):
                dxs = [e[0] - e[3] for e in entries]
                dys = [e[1] - e[4] for e in entries]
                mdx = sum(dxs) / 3.0
                mdy = sum(dys) / 3.0
                units = config_value("swipe_units")
                if abs(mdx) >= abs(mdy):
                    mag, other, ds = abs(mdx), abs(mdy), dxs
                    units *= 1.5           # horizontal swipes must be deliberate
                    margin = 2.0           # and clearly horizontal
                else:
                    mag, other, ds = abs(mdy), abs(mdx), dys
                    margin = 1.4
                same_dir = all(d > 0 for d in ds) or all(d < 0 for d in ds)
                if mag >= units and mag >= margin * other and same_dir:
                    if abs(mdx) >= abs(mdy):
                        direction = "right" if mdx > 0 else "left"
                    else:
                        direction = "down" if mdy > 0 else "up"  # PTP Y grows down
                    actions.put(SWIPE_ACTIONS[direction])
                    self._state = "fired"
                    self._swipe_fired = True
            # n=2/n=4 and state fired are ignored. Neither can start a new epoch.

    def _maintenance(self):
        """Timer-driven expiry also runs when the device sends no lift frame."""
        self._process_state(time.monotonic(), [])

    _input_stopped = False

    def fence_input(self):
        """Drain an in-flight frame and permanently reject new synthesis."""
        with CONFIG_LOCK:
            self._input_stopped = True

    def release_pending_button(self):
        """Retry only a pending synthetic LEFTUP, without hook cleanup."""
        with CONFIG_LOCK:
            if self._state in ("dragging", "grace"):
                self._end_drag()

    def abort_gesture(self):
        """Fail open on malformed input: never leave pointer/button blocked."""
        with CONFIG_LOCK:
            self._abort_gesture_locked()

    def _abort_gesture_locked(self):
        self._touch.clear()
        self._swipe_fired = False
        self._all_up_since = None
        errors = release_custom_input(
            self.release_pending_button,
            lambda: suppress_pointer.set(False),
        )
        if self._state in ("dragging", "grace"):
            # A failed LEFTUP remains visible so the next cleanup retries it.
            pass
        else:
            self._state = "idle"
        if errors:
            raise errors[0]

    def device_disconnected(self, hdev):
        """Fail open immediately when Raw Input reports device removal."""
        known = hdev in self._pp or hdev in self._is_tp or any(
            key[0] == hdev for key in self._touch
        )
        self._pp.pop(hdev, None)
        self._links.pop(hdev, None)
        self._is_tp.pop(hdev, None)
        if known:
            self.abort_gesture()

    # ---------------- raw input plumbing ----------------
    def _handle_raw_input(self, lparam):
        """Validate and dispatch one WM_INPUT payload or raise fail-open."""
        header_size = ctypes.sizeof(self.RAWINPUTHEADER)
        size = ctypes.c_uint(0)
        user32.GetRawInputData(
            ctypes.c_void_p(lparam), self.RID_INPUT, None,
            ctypes.byref(size), header_size,
        )
        if size.value < header_size + 8:
            raise ValueError("raw input payload is too short")

        buf = ctypes.create_string_buffer(size.value)
        copied = user32.GetRawInputData(
            ctypes.c_void_p(lparam), self.RID_INPUT, buf,
            ctypes.byref(size), header_size,
        )
        if copied == 0xFFFFFFFF or copied != size.value:
            raise OSError("GetRawInputData failed or returned a partial payload")

        hdr = ctypes.cast(buf, ctypes.POINTER(self.RAWINPUTHEADER)).contents
        if hdr.dwType != self.RIM_TYPEHID or not self._device_ok(hdr.hDevice):
            return

        two = ctypes.cast(
            ctypes.byref(buf, header_size), ctypes.POINTER(w.DWORD * 2)
        ).contents
        report_size, report_count = two[0], two[1]
        data_offset = header_size + 8
        data_end = data_offset + report_size * report_count
        if not report_size or data_end > len(buf):
            raise ValueError("raw HID report array exceeds its payload")

        for index in range(report_count):
            start = data_offset + index * report_size
            report = buf.raw[start:start + report_size]
            self._frame(hdr.hDevice, report)

    def _run(self):
        WNDPROCTYPE = ctypes.WINFUNCTYPE(ctypes.c_longlong, w.HWND, ctypes.c_uint,
                                         w.WPARAM, w.LPARAM)

        def wndproc(hwnd, msg, wp, lp):
            if msg == self.WM_INPUT_DEVICE_CHANGE and wp == self.GIDC_REMOVAL:
                run_input_callback(
                    lambda: self.device_disconnected(lp),
                    self.abort_gesture,
                    append_error,
                    "Raw Input device disconnect callback",
                )
                return 0
            if msg == self.WM_TIMER and wp == self.TIMER_ID:
                run_input_callback(
                    self._maintenance,
                    self.abort_gesture,
                    append_error,
                    "Raw Input maintenance callback",
                )
                return 0
            if msg == self.WM_INPUT:
                run_input_callback(
                    lambda: self._handle_raw_input(lp),
                    self.abort_gesture,
                    append_error,
                    "Raw Input callback",
                )
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        self._wndproc_ref = WNDPROCTYPE(wndproc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROCTYPE),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", w.HINSTANCE), ("hIcon", ctypes.c_void_p),
                        ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                        ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]

        user32.DefWindowProcW.argtypes = [w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_longlong
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = "PearipheralsTFD"
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "tfd", 0, 0, 0, 0, 0,
                                      None, None, hinst, None)
        rid = self.RAWINPUTDEVICE(
            self.UP_DIG, self.U_TOUCHPAD,
            self.RIDEV_INPUTSINK | self.RIDEV_DEVNOTIFY, hwnd,
        )
        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                              ctypes.sizeof(self.RAWINPUTDEVICE)):
            # Registration failed. Input behavior is unchanged (fail open); only
            # the observable state records that no listener could be registered.
            self._note_raw_input_state("unavailable")
            return  # no PTP device / registration failed — feature disabled
        if not user32.SetTimer(hwnd, self.TIMER_ID, 10, None):
            self._note_raw_input_state("unavailable")
            return
        self._note_raw_input_state("listener registered")
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def start(self):
        self._note_raw_input_state("starting")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()


three_finger_drag = ThreeFingerDrag()

# ---------------------------------------------------------------- touchpad settings
class TouchpadSettings:
    """Windows registry adapter around the reversible settings manager."""

    KEY = r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad"
    REQUIRED = TouchpadSettingsManager.NATIVE_GESTURES
    DRAG_MODE = TouchpadSettingsManager.CUSTOM_GESTURES
    RECOMMENDED = TouchpadSettingsManager.RECOMMENDED

    def __init__(self):
        self._manager = TouchpadSettingsManager(
            config=config,
            read_value=self._read,
            write_value=self._write,
            delete_value=self._delete,
            save=lambda: save_config(config),
        )

    def _read(self, name):
        def query():
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY) as key:
                return winreg.QueryValueEx(key, name)[0]

        return read_optional_value(query)

    def _write(self, name, value):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))

    def _delete(self, name):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass

    def status(self):
        return self.status_text() == "applied"

    def status_text(self):
        return self._manager.status_text(config_value("three_finger_mode"))

    def apply(self, include_recommended=True):
        return self._manager.apply(
            config_value("three_finger_mode"), include_recommended
        )

    def enforce(self):
        return self._manager.enforce(config_value("three_finger_mode"))

    def restore(self):
        return self._manager.restore()

    # ScrollDirection: 0 = content follows fingers (natural), 1 = classic
    # wheel direction. Windows reads it when the trackpad initializes.
    def natural_scroll_get(self):
        return self._read("ScrollDirection") == 0

    def natural_scroll_set(self, natural):
        with CONFIG_LOCK:
            config["natural_scroll"] = bool(natural)
            changed = self._manager.apply(config_value("three_finger_mode"))
        try:
            HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
            user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0,
                r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad",
                0x0002, 1000, ctypes.byref(ctypes.c_ulong()))
        except Exception:
            pass
        return changed


tp_settings = TouchpadSettings()

# ---------------------------------------------------------------- autostart
import winreg

# Read-only observation of the AmtPtpHidFilter service registration. The
# presence of this key means Windows has a service registered under that name.
# It does NOT mean the driver is installed correctly, loaded, running, healthy,
# or bound to any device, and the probe never says otherwise.
DRIVER_SERVICE_KEY = r"SYSTEM\CurrentControlSet\Services\AmtPtpHidFilter"


def driver_service_state():
    """Return only `service registration detected`/`not detected`/`unavailable`.

    The probe is read-only and total: any failure — including an unexpected
    non-OSError — is reported as uncertainty, and no exception text, error
    code, or registry path ever reaches the caller.
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, DRIVER_SERVICE_KEY, 0, winreg.KEY_READ
        ):
            return "service registration detected"
    except FileNotFoundError:
        return "not detected"
    except Exception:
        return "unavailable"


def autostart_command():
    """Return the exact command this installation should register."""
    if IS_FROZEN:
        return f'"{os.path.abspath(sys.executable)}"'
    pyw = os.path.join(APP_DIR, ".venv", "Scripts", "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def autostart_state():
    """Classify the Run value without ever guessing absence.

    Only a confirmed FileNotFoundError (missing key or missing value) means
    "off". Every other read failure is uncertainty, reported as "unavailable"
    so the status surface never claims autostart is disabled when it simply
    could not be read.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command = winreg.QueryValueEx(key, RUN_NAME)[0]
    except FileNotFoundError:
        return "off"
    except OSError:
        return "unavailable"
    return classify_autostart(command, autostart_command())


def autostart_get():
    """Preserved boolean API: True only when a Run value is actually present."""
    return autostart_state() in ("current", "stale")


def autostart_set(enable):
    cmd = autostart_command()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        RUN_KEY,
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
    ) as k:
        if enable:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
            return
        # Disabling must clear every name this app has ever registered under.
        # Only confirmed absence is success; any other failure is reported so a
        # caller can never treat a partial removal as complete. Each name is
        # still attempted, so one denial cannot hide the remaining entries.
        failure = None
        for name in (RUN_NAME, *OLD_RUN_NAMES):
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if failure is None:
                    failure = exc
        # Deleting without reading back would let a silently-ignored delete be
        # reported to the user as "removed and verified". Confirm absence.
        for name in (RUN_NAME, *OLD_RUN_NAMES):
            try:
                winreg.QueryValueEx(k, name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if failure is None:
                    failure = exc
                continue
            if failure is None:
                failure = OSError(
                    f"start-with-Windows entry still present after deletion: {name}"
                )
        if failure is not None:
            raise failure


def legacy_autostart_state():
    """Report whether ANY legacy Run value is still registered.

    "absent" only when every legacy name is confirmed missing. A read failure
    is "unavailable" so removal readiness can never claim a clean machine it
    could not actually verify. No command string is ever returned.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            for name in OLD_RUN_NAMES:
                try:
                    winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                except OSError:
                    return "unavailable"
                return "present"
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unavailable"
    return "absent"


def migrate_legacy_autostart():
    """Replace legacy Run entries with the current one, never delete-first.

    The current command is written and read back before a single legacy value
    is removed, so a failed or unverifiable write leaves every legacy entry
    intact and the machine still starts the app. Each legacy name is attempted
    independently, so one denial cannot hide the rest; partial cleanup stays
    observable through About / status and is retried on the next launch.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            present = []
            for name in OLD_RUN_NAMES:
                try:
                    winreg.QueryValueEx(k, name)
                except FileNotFoundError:
                    continue
                except OSError:
                    return
                present.append(name)
            if not present:
                return
            cmd = autostart_command()
            try:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
                registered = winreg.QueryValueEx(k, RUN_NAME)[0]
            except OSError as exc:
                append_error(f"Legacy autostart replacement could not be written: {exc}")
                return
            if classify_autostart(registered, cmd) != "current":
                append_error(
                    "Legacy autostart replacement could not be verified; "
                    "legacy entries were left in place."
                )
                return
            for name in present:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    append_error(f"Legacy autostart entry could not be removed: {exc}")
    except FileNotFoundError:
        return
    except OSError:
        return


# ---------------------------------------------------------------- tray
class WindowsTrayMenuRefreshDispatcher:
    """Marshal coalesced menu rebuilds onto pystray's Win32 message loop.

    pystray 0.19.5 exposes its HWND and message-handler table on the Windows
    backend. Posting one private WM_APP message lets the HID worker request a
    rebuild without running any pystray code itself.
    """

    WM_APP = 0x8000
    message = WM_APP + 0x4D5

    def __init__(self, icon, post_message, on_refresh=None):
        self._icon = icon
        self._post_message = post_message
        self._refresh_callback = on_refresh
        self._lock = threading.Lock()
        self._started = False
        self._pending = False
        self._closed = False

    def start(self):
        """Attach the callback after pystray has created its tray HWND."""
        with self._lock:
            if self._closed:
                return False
            if self._started:
                return True
            if self._icon._hwnd is None:
                raise RuntimeError("pystray tray window is not ready")
            self._icon._message_handlers[self.message] = self._on_refresh
            self._started = True
            return True

    def schedule(self):
        """Post at most one outstanding refresh message from any thread."""
        with self._lock:
            if self._closed or not self._started or self._pending:
                return False
            self._pending = True
            hwnd = self._icon._hwnd
        try:
            posted = self._post_message(hwnd, self.message, 0, 0)
        except Exception:
            posted = False
        if posted:
            return True
        with self._lock:
            self._pending = False
        return False

    def _on_refresh(self, wparam, lparam):
        """Run by pystray's window procedure on the tray/UI thread."""
        with self._lock:
            if self._closed:
                self._pending = False
                return
            self._pending = False
        try:
            self._icon.update_menu()
        except Exception:
            pass
        with self._lock:
            if self._closed:
                return
        if self._refresh_callback is not None:
            self._refresh_callback()

    def close(self):
        """Prevent new posts and make an already-posted callback a no-op."""
        with self._lock:
            self._closed = True
            self._pending = False


battery_snapshots = BatterySnapshotStore()
battery_alert_policy = BatteryAlertPolicy()
battery_stop = threading.Event()
battery_lifecycle_lock = threading.Lock()
battery_shutdown = False
battery_thread = None
battery_poller = None
battery_menu_refresh = None


def publish_battery_snapshot(snapshot):
    """Atomically publish battery state, then request a tray-thread rebuild."""
    battery_snapshots.publish(snapshot)
    if battery_menu_refresh is not None:
        battery_menu_refresh.schedule()


def notify_battery_alerts(icon):
    """Evaluate the latest complete snapshot only on the tray thread."""
    if battery_stop.is_set():
        return
    alerts = battery_alert_policy.evaluate(
        battery_snapshots.snapshot(), time.monotonic()
    )
    if alerts and _notify(icon, "\n".join(alerts) + "\nConnect to charge.",
                          "Pearipherals: low battery"):
        battery_alert_policy.acknowledge()


def battery_menu_label(device):
    """Read a complete snapshot when pystray evaluates a dynamic menu title."""
    snapshot = battery_snapshots.snapshot()
    if device == "mouse":
        return format_battery_label("Magic Mouse", snapshot.mouse)
    if device == "keyboard":
        return format_battery_label("Magic Keyboard", snapshot.keyboard)
    if device == "trackpad":
        return format_battery_label("Magic Trackpad", snapshot.trackpad)
    raise ValueError(f"unknown battery device: {device}")


MOUSE_SCROLL_DRIVER_URL = (
    "https://github.com/dinozucchini4-cmyk/"
    "Apple-Input-Device-Drivers-Windows-10-11/tree/master/AppleWirelessMouse"
)
TRACKPAD_DRIVER_URL = (
    "https://github.com/vitoplantamura/"
    "MagicTrackpad2ForWindows/releases/tag/v2.0"
)


def on_mouse_scroll_guide(icon, item):
    """Open the user-reviewed Apple driver package page; never install a driver."""
    _ = item
    try:
        opened = webbrowser.open(MOUSE_SCROLL_DRIVER_URL)
    except Exception as exc:
        append_error(f"Magic Mouse scroll guide could not open: {exc}")
        opened = False
    if not opened:
        _notify(
            icon,
            "The browser could not be opened. The driver guide is in the "
            "AppleWirelessMouse folder of the Apple Input Device Drivers repository.",
            "Magic Mouse scroll guide",
        )


def on_trackpad_driver_guide(icon, item):
    """Open the signed Magic Trackpad 2 driver release; never install it here."""
    _ = item
    try:
        opened = webbrowser.open(TRACKPAD_DRIVER_URL)
    except Exception as exc:
        append_error(f"Magic Trackpad driver guide could not open: {exc}")
        opened = False
    if not opened:
        _notify(
            icon,
            "The browser could not be opened. Find MagicTrackpad2ForWindows v2.0 "
            "in the upstream project's Releases page.",
            "Magic Trackpad 2 driver guide",
        )


def start_battery_worker():
    """Start isolated slow HID polling; never call pystray from this worker."""
    global battery_stop, battery_thread, battery_poller
    with battery_lifecycle_lock:
        if battery_shutdown:
            return
        if battery_thread is not None and battery_thread.is_alive():
            return
        battery_stop = threading.Event()
        try:
            import hid as hidapi
            battery_poller = BatteryPoller(
                HidBatteryBackend(hidapi),
                publish_battery_snapshot,
                trackpad_driver_battery_query=query_amt_ptp_driver_battery,
            )
            battery_thread = threading.Thread(
                target=battery_poller.run, args=(battery_stop,), daemon=True,
                name="PearipheralsBattery",
            )
            battery_thread.start()
        except Exception as exc:
            battery_stop.set()
            battery_thread = None
            battery_poller = None
            unavailable = BatteryResult("unavailable")
            publish_battery_snapshot(
                BatterySnapshot(
                    unavailable, unavailable, time.monotonic(), unavailable
                )
            )
            append_error(f"Battery worker startup failed: {exc}")


def stop_battery_worker(timeout=2.0):
    """Interrupt polling sleep and bound shutdown if hidapi is in a system call."""
    global battery_thread, battery_shutdown
    with battery_lifecycle_lock:
        battery_shutdown = True
        battery_stop.set()
        worker = battery_thread
    if worker is None:
        return True
    # The worker may need lifecycle state while exiting; never join under lock.
    worker.join(timeout)
    if worker.is_alive():
        append_error("Battery worker did not stop before the shutdown deadline")
        return False
    with battery_lifecycle_lock:
        if battery_thread is worker:
            battery_thread = None
    return True


def windows_release_info():
    """Return safe (version, build, architecture) strings for the About view.

    Windows 11 intentionally retains the NT 10.0 kernel version, and Python's
    ``platform.release()`` can therefore return ``"10"``. Build 22000 is the
    documented Windows 11 boundary; use it only to correct that marketing
    label, while preserving the complete build string for diagnostics.
    """
    system = platform.system()
    release = platform.release()
    version = platform.version()
    label = f"{system} {release}".strip()
    if system == "Windows" and release == "10":
        try:
            if int(version.split(".")[2]) >= 22000:
                label = "Windows 11"
        except (IndexError, ValueError):
            pass
    return label, version, platform.machine()


def build_identity():
    """Return the exact identity of this running build.

    Frozen builds carry a `git:<revision>` manifest produced at packaging time.
    A one-file build extracts bundled data to `sys._MEIPASS`, which is NOT the
    directory holding the executable. When `_MEIPASS` exists it is authoritative:
    a missing or invalid embedded manifest fails closed to unknown and never
    falls back to an externally writable file beside the executable. `APP_DIR`
    is used only by frozen one-folder layouts that have no `_MEIPASS`.

    Source mode falls back to an ENTRY-SCRIPT identity, which measures only this
    file and is explicitly not a complete source-build identity. Any failure
    degrades to the fixed unknown identity: no path or error text is surfaced.
    """
    if IS_FROZEN:
        try:
            if hasattr(sys, "_MEIPASS"):
                directory = os.fspath(sys._MEIPASS)
                if (
                    not directory
                    or not os.path.isabs(directory)
                    or (os.name == "nt" and not os.path.splitdrive(directory)[0])
                ):
                    raise ValueError("invalid embedded manifest directory")
            else:
                directory = APP_DIR
            with open(os.path.join(directory, BUILD_MANIFEST_NAME), "r",
                      encoding="utf-8") as stream:
                manifest = json.load(stream)
            return BuildIdentity(manifest["version"], manifest["build_id"])
        except Exception:
            return BuildIdentity(APP_VERSION, UNKNOWN_BUILD_ID)
    try:
        return BuildIdentity(APP_VERSION, entry_source_build_id(os.path.abspath(__file__)))
    except Exception:
        return BuildIdentity(APP_VERSION, UNKNOWN_BUILD_ID)


def _safe_state(read, fallback="unavailable"):
    """Return a probe's value, or a fixed fallback. Never leaks failure text."""
    try:
        return read()
    except Exception:
        return fallback


def build_support_snapshot():
    """Compose the About/diagnostics snapshot from cached, conservative state.

    Reads exactly one already-published battery snapshot; menu rendering must
    never trigger HID enumeration. Every probe degrades to a fixed enum value,
    so no exception text, path, or device identifier can reach the surface.
    """
    version, build, architecture = _safe_state(
        windows_release_info, ("unavailable", "unavailable", "unavailable")
    )
    battery = battery_snapshots.snapshot()
    gesture_mode = _safe_state(
        lambda: config_value("three_finger_mode", "off"), "off"
    )
    touchpad_settings = _safe_state(tp_settings.status_text)
    scroll_direction = _safe_state(
        lambda: "natural" if tp_settings.natural_scroll_get() else "classic"
    )
    autostart = _safe_state(autostart_state)
    legacy_autostart = _safe_state(legacy_autostart_state, "unavailable")
    raw_input = _safe_state(three_finger_drag.raw_input_state, "unavailable")
    if gesture_mode == "off":
        gesture_readiness = "off"
    elif touchpad_settings in (
        "needs apply", "original settings", "restore incomplete"
    ):
        gesture_readiness = "action required"
    elif touchpad_settings != "applied":
        gesture_readiness = "unavailable"
    elif raw_input == "listener registered":
        gesture_readiness = "ready"
    elif raw_input in ("not started", "starting"):
        gesture_readiness = raw_input
    else:
        gesture_readiness = "unavailable"
    settings_applied = _safe_state(
        lambda: bool(config_value("tp_settings_applied", False)), True
    )
    mac_fkeys = _safe_state(lambda: bool(config_value("mac_fkeys", True)), True)
    backup_entries = _safe_state(
        lambda: dict(config_value("tp_settings_backup") or {}), {"unknown": 1}
    )
    return SupportSnapshot(
        identity=_safe_state(
            build_identity, BuildIdentity(APP_VERSION, UNKNOWN_BUILD_ID)
        ),
        mode="frozen" if IS_FROZEN else "source",
        windows_version=version,
        windows_build=build,
        architecture=architecture,
        config_state="loaded" if CONFIG_RECOVERY_ERROR is None else "recovery required",
        keyboard_battery=battery_state(battery.keyboard),
        trackpad_battery=battery_state(battery.trackpad),
        raw_input_state=raw_input,
        driver_state=_safe_state(driver_service_state),
        gesture_mode=gesture_mode,
        gesture_readiness=gesture_readiness,
        touchpad_settings=touchpad_settings,
        scroll_direction=scroll_direction,
        autostart=autostart,
        lifecycle=LIFECYCLE_STATE,
        removal_readiness=classify_removal_readiness(
            autostart, legacy_autostart, mac_fkeys, gesture_mode,
            settings_applied, backup_entries
        ),
    )


def show_about_dialog():
    """Show one serialized, ownerless About/status dialog.

    Called only from an explicit tray callback. MessageBoxW runs on a temporary
    clean thread so pystray's hidden GUI windows cannot retain activation or
    focus. Any failure is logged rather than raised so a missing window station
    can never take the tray down.
    """
    if not DIALOG_LOCK.acquire(blocking=False):
        return
    outcome = {}
    finished = threading.Event()

    def dialog_worker():
        try:
            outcome["answer"] = user32.MessageBoxW(
                None,
                format_about(build_support_snapshot()),
                "Pearipherals — About / status",
                MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND,
            )
        except Exception as exc:
            outcome["error"] = exc
        finally:
            finished.set()

    try:
        worker = threading.Thread(
            target=dialog_worker, name="PearipheralsDialog", daemon=True
        )
        worker.start()
        finished.wait()
        worker.join()
        if "error" in outcome:
            raise outcome["error"]
    except Exception as exc:
        append_error(f"About / status dialog failed: {exc}")
    finally:
        DIALOG_LOCK.release()


def on_about(icon, item):
    """Explicit tray action; nothing is shown until the user clicks it."""
    show_about_dialog()


def open_support_link(url):
    """Open one of the fixed project URLs in the user's browser.

    Only the exact DOCS_URL/REPORT_URL constants are accepted, so no computed,
    user-supplied, or redirected destination can ever be launched. No network
    access happens until this runs from an explicit tray click.
    """
    if url not in (DOCS_URL, REPORT_URL):
        append_error("Refused to open a link that is not a fixed project URL")
        return
    try:
        webbrowser.open(url)
    except Exception:
        # Never echo the failure text: a browser error can contain local paths.
        append_error("Could not open the project link in a browser")


def on_open_docs(icon, item):
    """Explicit tray action; the browser opens only on this click."""
    open_support_link(DOCS_URL)


def on_report_problem(icon, item):
    """Explicit tray action; the browser opens only on this click."""
    open_support_link(REPORT_URL)


def export_diagnostics(icon):
    """Write one local diagnostic report beside the app, on explicit request.

    Nothing is generated, written, or transmitted until the user picks the
    menu item. The report is whitelist-only and stays on disk; the notice
    names the fixed filename so no local path is ever put on screen.
    """
    path = os.path.join(APP_DIR, DIAGNOSTICS_FILENAME)
    try:
        payload = diagnostics_payload(
            build_support_snapshot(), datetime.datetime.now(datetime.timezone.utc)
        )
        save_json_atomic(path, payload)
    except Exception as exc:
        append_error(f"Diagnostic report could not be saved: {exc}")
        _notify(
            icon,
            "The report could not be saved next to Pearipherals. Check that "
            "the app folder is writable, then try again.",
            "Diagnostic report failed",
        )
        return
    _notify(
        icon,
        f"Saved {DIAGNOSTICS_FILENAME} in the Pearipherals folder. Review it "
        "before sharing it; Pearipherals never uploads it.",
        "Diagnostic report saved",
    )


def on_export_diagnostics(icon, item):
    """Explicit tray action; no report exists until the user clicks this."""
    export_diagnostics(icon)


def confirm_removal():
    """Ask before any removal mutation. Anything but an explicit Yes cancels.

    The ownerless dialog runs on a temporary clean thread, uses the pointer-sized
    MessageBoxW prototype, and pre-selects No (MB_DEFBUTTON2). Cancel and X are
    safe cancellation results. A dialog failure is cancellation, never consent.
    """
    if not DIALOG_LOCK.acquire(blocking=False):
        return False
    outcome = {}
    finished = threading.Event()

    def dialog_worker():
        try:
            outcome["answer"] = user32.MessageBoxW(
                None,
                "Prepare Pearipherals for removal?\n\n"
                "It will turn off the Mac F-row and three-finger gestures, remove "
                "its start-with-Windows entries, and put your original Windows "
                "touchpad settings back.\n\n"
                "No files are deleted and Pearipherals keeps running until you "
                "choose Quit. Choose No, Cancel, or X to leave everything as it is.",
                "Pearipherals — Prepare for removal",
                MB_YESNOCANCEL | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND,
            )
        except Exception as exc:
            outcome["error"] = exc
        finally:
            finished.set()

    try:
        worker = threading.Thread(
            target=dialog_worker, name="PearipheralsDialog", daemon=True
        )
        worker.start()
        finished.wait()
        worker.join()
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("answer") == IDYES
    except Exception as exc:
        append_error(f"Removal confirmation dialog failed: {exc}")
        return False
    finally:
        DIALOG_LOCK.release()


def on_prepare_removal(icon, item):
    """Reverse every persistent change this app made, after explicit consent.

    Nothing happens without a Yes. A recovery-required config blocks automatic
    mutation entirely and explains the manual steps instead. Each step is
    independent, so an early failure cannot skip the remaining cleanup, and a
    partial run is never reported as success. No file is deleted and the app is
    never stopped: the user chooses Quit when they are ready.
    """
    if not confirm_removal():
        return
    if CONFIG_RECOVERY_ERROR is not None:
        _notify(
            icon,
            "The settings file needs repair, so Pearipherals will not change "
            "anything automatically. Untick Start with Windows, set the "
            "3-finger gesture to Off, and use Restore original Windows "
            "settings, then Quit.",
            "Manual removal steps required",
        )
        return

    def disable_settings():
        with CONFIG_LOCK:
            config["mac_fkeys"] = False
            config["three_finger_mode"] = "off"
            config["tp_settings_applied"] = False
            save_config(config)

    input_errors = []

    def release_input():
        input_errors.extend(release_custom_input(
            three_finger_drag.abort_gesture,
            lambda: suppress_pointer.set(False),
        ))
        if input_errors:
            raise input_errors[0]

    def restore_touchpad():
        # Other removal steps remain independent, but native ownership must
        # wait for successful input cleanup. Retain originals for a retry.
        if input_errors:
            raise input_errors[0]
        return tp_settings.restore()

    outcome = perform_removal({
        "settings": disable_settings,
        "input_release": release_input,
        "autostart": lambda: autostart_set(False),
        "touchpad": restore_touchpad,
    })
    _notify(
        icon,
        format_removal_summary(outcome),
        "Pearipherals — Prepare for removal",
    )
    _update_menu(icon)


def make_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 16, 60, 48], radius=8, fill=(240, 240, 245, 255))
    for x in (14, 26, 38):
        d.rectangle([x, 24, x + 8, 32], fill=(60, 60, 70, 255))
    d.rectangle([14, 36, 50, 42], fill=(60, 60, 70, 255))
    d.ellipse([44, 6, 62, 24], fill=(255, 176, 32, 255))
    d.text((49, 8), "F", fill=(40, 40, 40, 255))
    return img


def on_toggle(icon, item):
    with CONFIG_LOCK:
        config["mac_fkeys"] = not config_value("mac_fkeys")
        save_config(config)


def on_autostart(icon, item):
    autostart_set(not autostart_get())


def set_tf_mode(mode):
    # Custom modes must own the gesture exclusively. Off mode restores native
    # handling only while the user has explicitly applied managed settings;
    # after Restore, off mode leaves the original registry values untouched.
    with CONFIG_LOCK:
        previous_mode = config["three_finger_mode"]
        try:
            return set_managed_mode(
                config,
                mode,
                enforce=lambda requested_mode: tp_settings.enforce(),
                save=lambda: save_config(config),
            )
        finally:
            if config["three_finger_mode"] != previous_mode:
                # Serialize the entire transition with frames and timer ticks.
                # Failed LEFTUP remains pending for maintenance/Quit retry.
                release_custom_input(
                    three_finger_drag.abort_gesture,
                    lambda: suppress_pointer.set(False),
                )


def _update_menu(icon):
    try:
        icon.update_menu()
    except Exception:
        pass


def _notify(icon, message, title):
    """Best effort: True means no Python exception, not confirmed delivery."""
    try:
        icon.notify(message, title)
    except Exception:
        return False
    return True


def on_mode_swipes(icon, item):
    set_tf_mode("swipes")
    _update_menu(icon)


def on_mode_drag(icon, item):
    set_tf_mode("drag")
    _update_menu(icon)


def on_mode_off(icon, item):
    set_tf_mode("off")
    release_custom_input(
        three_finger_drag.abort_gesture,
        lambda: suppress_pointer.set(False),
    )
    _update_menu(icon)


def on_natural_scroll(icon, item):
    try:
        changed = tp_settings.natural_scroll_set(
            not tp_settings.natural_scroll_get()
        )
    except Exception as exc:
        _notify(icon, f"Could not save scroll direction: {exc}",
                "Scroll direction failed")
    else:
        _notify(icon,
                "Flip the trackpad's power switch off/on (2 s) to apply — "
                "or it applies at next reboot. "
                f"Updated {len(changed)} setting(s).",
                "Scroll direction saved")
    _update_menu(icon)


def on_apply_tp(icon, item):
    try:
        changed = tp_settings.apply()
    except Exception as exc:
        _notify(icon, f"Could not apply settings: {exc}",
                "Touchpad settings failed")
    else:
        detail = (f"Updated {len(changed)} setting(s)."
                  if changed else "Settings were already correct.")
        _notify(icon,
                detail + " Reconnect the trackpad if Windows does not update immediately.",
                "Touchpad settings applied")
    _update_menu(icon)


def on_restore_tp(icon, item):
    try:
        # Fence new synthesis before cleanup, and keep frames/mode changes out
        # until native ownership has been restored (or recovery retained).
        with CONFIG_LOCK:
            config["three_finger_mode"] = "off"
            errors = release_custom_input(
                three_finger_drag.abort_gesture,
                lambda: suppress_pointer.set(False),
            )
            if errors:
                # Never hand native ownership back while LEFTUP or hook
                # removal is pending. Keep every original for an explicit
                # Restore retry; maintenance can still retry the button.
                config["tp_settings_applied"] = False
                try:
                    save_config(config)
                except Exception as exc:
                    errors.append(exc)
                raise RuntimeError("; ".join(str(error) for error in errors))
            restored = tp_settings.restore()
    except Exception as exc:
        _notify(icon, f"Could not restore settings: {exc}",
                "Touchpad restore failed")
    else:
        detail = (f"Restored {len(restored)} original setting(s)."
                  if restored else "No saved changes remained to restore.")
        _notify(icon, detail + " Custom 3-finger handling is now off.",
                "Original touchpad settings restored")
    _update_menu(icon)


def first_run_setup():
    """One-click onboarding: called when no config file exists yet.

    - enable autostart from the current location
    - apply required touchpad settings (originals backed up in config)

    Returns True when this really was the first run, so the caller can
    announce it instead of leaving the user to guess.
    """
    if CONFIG_RECOVERY_ERROR is not None:
        return False
    if config_value("setup_done"):
        return False
    try:
        autostart_set(True)
    except Exception as exc:
        # Autostart is the one promise onboarding makes about persistence.
        # If it cannot be kept, leave onboarding pending, mutate no touchpad
        # setting, and emit no success message; the next launch retries.
        append_error(f"First-run autostart could not be enabled: {exc}")
        return False
    try:
        tp_settings.apply()
    except Exception:
        # Keep onboarding pending and suppress the success toast. Apply already
        # fails custom gesture ownership closed when registry mutation fails.
        return False
    with CONFIG_LOCK:
        config["setup_done"] = True
        save_config(config)
    return True


def tray_setup(icon, first_run):
    """pystray setup hook, run in its own thread once the loop is up.

    A custom setup callback owns `visible` — pystray only sets it for you
    when no callback is given.

    First launch turns on autostart and rewrites touchpad settings without
    asking; say so once, rather than leaving the tray icon to be discovered
    by accident. A toast and not a dialog: this starts at logon, so nothing
    here should steal focus.

    The wording states only what actually completed. It never claims a Magic
    Keyboard, Magic Trackpad, or driver was found: nothing here probes for a
    device, and a sleeping Bluetooth device would make any such claim a lie.
    """
    icon.visible = True
    if not battery_menu_refresh.start():
        return
    if CONFIG_RECOVERY_ERROR is not None:
        _notify(
            icon,
            "pearipherals.json is malformed or unreadable. The original was "
            "preserved and automatic settings changes are disabled. Move or "
            "repair that file, then restart Pearipherals.",
            "Config recovery required",
        )
    elif first_run:
        import time as _t
        _t.sleep(2)            # let the tray icon register before we toast it
        if battery_stop.is_set() or not battery_menu_refresh.start():
            return
        _notify(icon,
                "The Mac F-row and 3-finger swipes are turned on, "
                "recommended touchpad settings were applied, and "
                "Pearipherals now starts with Windows. Right-click this "
                "icon to change any of it, or open About / status to see "
                "what it can currently see.",
                "Pearipherals is running")
    # The HWND/handler and startup messages must precede the first battery
    # result, so onboarding cannot replace an initial low-battery balloon.
    if not battery_stop.is_set() and battery_menu_refresh.start():
        start_battery_worker()


def on_quit(icon, item):
    # Fence before any cleanup/join can yield to the Raw Input listener. Keep
    # the saved mode unchanged; only this process is shutting down.
    three_finger_drag.fence_input()
    actions.put(None)
    if battery_menu_refresh is not None:
        battery_menu_refresh.close()
    shutdown_errors = shutdown_custom_input(
        three_finger_drag.abort_gesture,
        lambda: suppress_pointer.set(False),
        suppress_pointer.shutdown,
        retry_release=three_finger_drag.release_pending_button,
    )
    stop_battery_worker()
    if shutdown_errors:
        detail = "; ".join(str(error) for error in shutdown_errors)
        _notify(icon, detail, "Input release failed on Quit")
        try:
            log_path = os.path.join(APP_DIR, "pearipherals.err.log")
            with open(log_path, "a", encoding="utf-8") as stream:
                stream.write(
                    f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"Input release failed on Quit: {detail}\n"
                )
        except Exception:
            pass
    try:
        if brightness.last_backend == "software dim":
            brightness.gamma.restore()
    except Exception:
        pass
    icon.stop()
    os._exit(0)


def main():
    global tray_icon, battery_menu_refresh
    # Migrate old autostart names only when the primary config is trustworthy.
    # This writes and verifies the current entry before removing any legacy
    # one, so a machine can never be left with no way to start the app.
    if CONFIG_RECOVERY_ERROR is None:
        migrate_legacy_autostart()
    is_first_run = first_run_setup()
    # Re-assert only settings the user still asked Pearipherals to manage.
    # A recovery-required primary config disables all automatic registry writes.
    if CONFIG_RECOVERY_ERROR is None:
        try:
            set_tf_mode(config_value("three_finger_mode"))
        except Exception:
            # set_tf_mode has already durably failed closed to off.
            pass
    # portable self-heal: if autostart is on but we've been moved, re-point it
    if CONFIG_RECOVERY_ERROR is None and autostart_get():
        try:
            autostart_set(True)
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=hook_thread, daemon=True).start()
    suppress_pointer.start()
    three_finger_drag.start()
    tray_icon = pystray.Icon(
        "Pearipherals", make_icon(),
        "Pearipherals — Tragic Keyboard + Tragic Trackpad",
        menu=pystray.Menu(
            pystray.MenuItem("Tragic Keyboard (F1-F12 → media/brightness)",
                             on_toggle,
                             checked=lambda i: config_value("mac_fkeys")),
            pystray.MenuItem("Tragic Trackpad — 3-finger gesture", pystray.Menu(
                pystray.MenuItem("Swipes (Task View / minimize / restore)",
                                 on_mode_swipes, radio=True,
                                 checked=lambda i: config_value("three_finger_mode") == "swipes"),
                pystray.MenuItem("Drag (move windows / select like a Mac)",
                                 on_mode_drag, radio=True,
                                 checked=lambda i: config_value("three_finger_mode") == "drag"),
                pystray.MenuItem("Off (let Windows handle it)",
                                 on_mode_off, radio=True,
                                 checked=lambda i: config_value("three_finger_mode") == "off"),
            )),
            pystray.MenuItem("Natural scrolling", on_natural_scroll,
                             checked=lambda i: tp_settings.natural_scroll_get()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: battery_menu_label("mouse"),
                None, enabled=False),
            pystray.MenuItem(
                lambda item: battery_menu_label("keyboard"),
                None, enabled=False),
            pystray.MenuItem(
                lambda item: battery_menu_label("trackpad"),
                None, enabled=False),
            pystray.MenuItem(
                "Magic Mouse scrolling setup guide", on_mouse_scroll_guide),
            pystray.MenuItem(
                "Magic Trackpad 2 signed driver guide", on_trackpad_driver_guide),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Touchpad settings", pystray.Menu(
                pystray.MenuItem(
                    lambda item: f"Status: {tp_settings.status_text()}",
                    None, enabled=False),
                pystray.MenuItem("Apply recommended settings", on_apply_tp),
                pystray.MenuItem("Restore original Windows settings", on_restore_tp),
            )),
            pystray.MenuItem("Start with Windows", on_autostart,
                             checked=lambda i: autostart_get()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("About / status…", on_about),
            pystray.MenuItem("Save a diagnostic report", on_export_diagnostics),
            pystray.MenuItem("Open documentation", on_open_docs),
            pystray.MenuItem("Report a problem", on_report_problem),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Prepare for removal…", on_prepare_removal),
            pystray.MenuItem("Quit", on_quit),
        ))
    battery_menu_refresh = WindowsTrayMenuRefreshDispatcher(
        tray_icon, user32.PostMessageW,
        on_refresh=lambda: notify_battery_alerts(tray_icon),
    )
    tray_icon.run(setup=lambda icon: tray_setup(icon, is_first_run))


def _single_instance_or_exit():
    """Named mutex so double-starts (Run key + manual) can't stack hooks.

    The pre-rename mutex is checked too, so upgrading over a still-running
    MagicSuite.exe doesn't end up with two copies hooking the same keys."""
    ERROR_ALREADY_EXISTS = 183
    for name in ("Pearipherals_single_instance_v1",
                 "MagicSuite_single_instance_v2"):
        kernel32.CreateMutexW(None, False, name)
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            os._exit(0)


if __name__ == "__main__":
    _single_instance_or_exit()
    log_path = os.path.join(APP_DIR, "pearipherals.err.log")
    # Early-logon resilience: explorer/tray may not exist yet, displays may
    # still be initializing. Retry the whole app a few times before giving up.
    import time as _time
    import traceback
    for attempt in range(5):
        try:
            main()
            break                      # clean exit via Quit
        except SystemExit:
            raise
        except Exception:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] "
                            f"attempt {attempt + 1} crashed:\n")
                    f.write(traceback.format_exc())
            except Exception:
                pass
            _time.sleep(10)
