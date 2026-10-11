"""Pure, testable core helpers for Pearipherals."""

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field


_SAVE_LOCK = threading.Lock()
CONFIG_LOCK = threading.RLock()


class ConfigRecoveryRequired(RuntimeError):
    """An existing primary config cannot be trusted or overwritten safely."""

    def __init__(self, path, cause):
        self.path = path
        self.cause = cause
        super().__init__(
            "Existing config is unreadable or malformed; original preserved at "
            f"{path}: {cause}"
        )


def load_json_config(path, defaults):
    """Load primary config, distinguishing absence from recovery-required I/O."""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            loaded = json.load(stream)
    except FileNotFoundError:
        return dict(defaults), "missing"
    except Exception as exc:
        raise ConfigRecoveryRequired(path, exc) from exc
    if not isinstance(loaded, dict):
        raise ConfigRecoveryRequired(path, ValueError("top level must be an object"))
    return {**defaults, **loaded}, "loaded"


APPLE_MAGIC_TRACKPAD_PID = "pid&0265"
APPLE_BLUETOOTH_PATH_VID = "vid&0001004c"


def read_optional_value(read):
    """Map a confirmed missing value to None without hiding read failures."""
    try:
        return read()
    except FileNotFoundError:
        return None


def require_single_input(send_input):
    """Require one synthetic input event to have been accepted by Windows."""
    if send_input() != 1:
        raise OSError("SendInput did not accept the synthetic input event")


def set_managed_mode(config, mode, enforce, save, lock=CONFIG_LOCK):
    """Persist and enforce a mode, rolling custom handling off on any failure."""
    with lock:
        config["three_finger_mode"] = mode
        try:
            save()
            changed = enforce(mode)
        except Exception:
            config["three_finger_mode"] = "off"
            config["tp_settings_applied"] = False
            try:
                save()
            except Exception:
                pass
            raise
        return changed


def release_custom_input(abort_gesture, disable_suppression):
    """Attempt every fail-open input release action, even if one fails."""
    errors = []
    for action in (abort_gesture, disable_suppression):
        try:
            action()
        except Exception as exc:
            errors.append(exc)
    return errors


def run_input_callback(action, cleanup, log, context):
    """Log callback failures and run fail-open cleanup without escaping Win32."""
    try:
        action()
    except Exception as exc:
        try:
            log(f"{context} failed: {exc}")
        except Exception:
            pass
        try:
            cleanup()
        except Exception as cleanup_exc:
            try:
                log(f"{context} cleanup failed: {cleanup_exc}")
            except Exception:
                pass


def shutdown_custom_input(
    abort_gesture,
    disable_suppression,
    stop_suppressor,
    retry_release=None,
    release_retries=2,
):
    """Release input, stop the hook, and retry a failed LEFTUP independently."""
    errors = []
    release_error = None
    try:
        abort_gesture()
    except Exception as exc:
        release_error = exc
    try:
        disable_suppression()
    except Exception as exc:
        errors.append(exc)
    try:
        stop_suppressor()
    except Exception as exc:
        errors.append(exc)

    retry_release = retry_release or abort_gesture
    for _ in range(release_retries if release_error is not None else 0):
        try:
            retry_release()
        except Exception as exc:
            release_error = exc
        else:
            release_error = None
            break
    if release_error is not None:
        errors.insert(0, release_error)
    return errors


def save_json_atomic(path, data):
    """Serialize a locked snapshot through a unique durable sibling temp file."""
    with CONFIG_LOCK:
        snapshot = deepcopy(data)
        with _SAVE_LOCK:
            directory = os.path.dirname(os.path.abspath(path))
            prefix = f".{os.path.basename(path)}."
            descriptor, temporary = tempfile.mkstemp(
                dir=directory, prefix=prefix, suffix=".tmp", text=True
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(snapshot, stream, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                try:
                    os.remove(temporary)
                except FileNotFoundError:
                    pass


def is_target_trackpad_path(device_path):
    """Accept only the Apple Magic Trackpad 2 precision-touchpad interface."""
    path = (device_path or "").lower()
    return APPLE_MAGIC_TRACKPAD_PID in path and APPLE_BLUETOOTH_PATH_VID in path


@dataclass(frozen=True)
class BatteryResult:
    status: str
    percentage: int | None = None
    charging: bool = False
    fully_charged: bool = False
    stale: bool = False
    last_percentage: int | None = None


@dataclass(frozen=True)
class BatterySnapshot:
    keyboard: BatteryResult
    trackpad: BatteryResult
    captured_at: float
    # Keep the original three positional arguments stable for existing callers/tests.
    # Mouse support is additive; older code gets a harmless loading state by default.
    mouse: BatteryResult = field(default_factory=lambda: BatteryResult("loading"))


class BatterySnapshotStore:
    """Publish complete immutable battery snapshots across threads."""

    def __init__(self):
        loading = BatteryResult("loading")
        self._snapshot = BatterySnapshot(loading, loading, 0.0)
        self._lock = threading.Lock()

    def publish(self, snapshot):
        if not isinstance(snapshot, BatterySnapshot):
            raise TypeError("battery update must be a BatterySnapshot")
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self):
        with self._lock:
            return self._snapshot


class BatteryAlertPolicy:
    """Pure per-device low-battery notification policy, owned by the tray thread."""

    def __init__(self):
        self._warned = {}
        self._pending = {}

    def evaluate(self, snapshot, now):
        """Stage one combined warning; only acknowledge a non-raising attempt."""
        self._pending = {}
        # Never turn a delayed tray callback into an alert for an old reading.
        if (type(snapshot.captured_at) not in (int, float)
                or type(now) not in (int, float)
                or not 0 <= now - snapshot.captured_at <= 600.0):
            return ()
        alerts = []
        for device, result in (("Magic Keyboard", snapshot.keyboard),
                               ("Magic Trackpad", snapshot.trackpad),
                               ("Magic Mouse", snapshot.mouse)):
            if (result.status != "available" or result.stale
                    or type(result.percentage) is not int
                    or not 0 <= result.percentage <= 100):
                continue
            if result.percentage >= 25:
                self._warned.pop(device, None)
            if result.percentage <= 20 and not (result.charging or result.fully_charged):
                level = 2 if result.percentage <= 5 else 1
                if level > self._warned.get(device, 0):
                    suffix = " (critical)" if level == 2 else ""
                    alerts.append(f"{device}: {result.percentage}%{suffix}")
                    self._pending[device] = level
        return tuple(alerts)

    def acknowledge(self):
        """Consume the last evaluation, not proof Windows displayed a balloon."""
        self._warned.update(self._pending)
        self._pending = {}


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


class _HIDP_VALUE_CAPS(ctypes.Structure):
    # Mirrors hidpi.h HIDP_VALUE_CAPS with Pack=4. For IsRange=False,
    # UsageMin occupies the union slot called Usage in the native structure.
    _pack_ = 4
    _fields_ = [
        ("UsagePage", ctypes.c_ushort),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", ctypes.c_ushort),
        ("LinkCollection", ctypes.c_ushort),
        ("LinkUsage", ctypes.c_ushort),
        ("LinkUsagePage", ctypes.c_ushort),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", ctypes.c_ushort),
        ("ReportCount", ctypes.c_ushort),
        ("Reserved1", ctypes.c_ushort),
        ("Reserved2", ctypes.c_ushort),
        ("Reserved3", ctypes.c_ushort),
        ("Reserved4", ctypes.c_ushort),
        ("Reserved5", ctypes.c_ushort),
        ("UnitsExp", ctypes.c_uint),
        ("Units", ctypes.c_uint),
        ("LogicalMin", wintypes.LONG),
        ("LogicalMax", wintypes.LONG),
        ("PhysicalMin", wintypes.LONG),
        ("PhysicalMax", wintypes.LONG),
        ("UsageMin", ctypes.c_ushort),
        ("UsageMax", ctypes.c_ushort),
        ("StringMin", ctypes.c_ushort),
        ("StringMax", ctypes.c_ushort),
        ("DesignatorMin", ctypes.c_ushort),
        ("DesignatorMax", ctypes.c_ushort),
        ("DataIndexMin", ctypes.c_ushort),
        ("DataIndexMax", ctypes.c_ushort),
    ]


def discover_generic_battery_feature(path):
    """Return (report_id, report_length) only if HID capabilities declare
    Generic Device / Battery Strength (Usage Page 0x0006, Usage 0x0020)
    as a FEATURE value capability on this exact HID collection.

    The probe only opens a HID handle for descriptor inspection. It does not
    write output or feature reports, change drivers, or modify the registry.
    """
    if os.name != "nt":
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    get_preparsed = hid.HidD_GetPreparsedData
    get_preparsed.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    get_preparsed.restype = wintypes.BOOL
    free_preparsed = hid.HidD_FreePreparsedData
    free_preparsed.argtypes = [ctypes.c_void_p]
    free_preparsed.restype = wintypes.BOOL
    get_caps = hid.HidP_GetCaps
    get_caps.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDP_CAPS)]
    get_caps.restype = wintypes.LONG
    get_value_caps = hid.HidP_GetValueCaps
    get_value_caps.argtypes = [
        ctypes.c_int, ctypes.POINTER(_HIDP_VALUE_CAPS),
        ctypes.POINTER(ctypes.c_ushort), ctypes.c_void_p,
    ]
    get_value_caps.restype = wintypes.LONG

    device_path = os.fsdecode(path)
    handle = create_file(
        device_path, 0, 0x00000001 | 0x00000002, None, 3, 0, None
    )
    handle_value = handle if isinstance(handle, int) else getattr(handle, "value", None)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle_value is None or handle_value == invalid_handle:
        return None

    preparsed = ctypes.c_void_p()
    try:
        if not get_preparsed(handle, ctypes.byref(preparsed)):
            return None

        caps = _HIDP_CAPS()
        if get_caps(preparsed, ctypes.byref(caps)) != 0x00110000:
            return None
        count_value = int(caps.NumberFeatureValueCaps)
        feature_length = int(caps.FeatureReportByteLength)
        if count_value <= 0 or feature_length < 2:
            return None

        array_type = _HIDP_VALUE_CAPS * count_value
        value_caps = array_type()
        count = ctypes.c_ushort(count_value)
        if get_value_caps(2, value_caps, ctypes.byref(count), preparsed) != 0x00110000:
            return None

        for cap in value_caps[:count.value]:
            if (
                cap.IsRange == 0
                and cap.UsagePage == 0x0006
                and cap.UsageMin == 0x0020
                and cap.ReportID >= 0
            ):
                return int(cap.ReportID), feature_length
        return None
    finally:
        if preparsed.value:
            free_preparsed(preparsed)
        close_handle(handle)


class HidBatteryBackend:
    """Small injected hidapi adapter with one handle lifetime per query."""

    def __init__(self, hid_module):
        self._hid = hid_module

    def enumerate_devices(self):
        return self._hid.enumerate()

    def request_report(self, path):
        handle = self._hid.device()
        try:
            handle.open_path(path)
            return bytes(handle.get_input_report(0x90, 3))
        finally:
            handle.close()

    def feature_report_info(self, path):
        """Inspect the collection descriptor; do not guess a feature report ID."""
        return discover_generic_battery_feature(path)

    def request_feature_report(self, path, report_id, length):
        """Read one descriptor-selected feature report; never sends a report."""
        handle = self._hid.device()
        try:
            handle.open_path(path)
            return bytes(handle.get_feature_report(report_id, length))
        finally:
            handle.close()


def parse_apple_battery_report(report):
    """Parse Apple's 3-byte current-state input report 0x90."""
    raw = bytes(report)
    if len(raw) != 3:
        raise ValueError("battery report must contain exactly 3 bytes")
    if raw[0] != 0x90:
        raise ValueError("unexpected battery report ID")
    if raw[2] > 100:
        raise ValueError("battery percentage is outside 0..100")
    return BatteryResult(
        "available",
        percentage=raw[2],
        charging=bool(raw[1] & 0x02),
        fully_charged=bool(raw[1] & 0x04),
    )


def query_apple_battery(
    product_id,
    enumerate_devices,
    request_report,
    request_feature_report=None,
    feature_report_info=None,
):
    """Read an Apple keyboard/trackpad battery using only descriptor-backed reports.

    Prefer Input 0x90 from the Apple vendor battery collection. If that input
    collection is not exposed by the active function driver, a Feature report
    is queried only when the same HID collection's descriptor declares Generic
    Device / Battery Strength (Usage Page 0x0006, Usage 0x0020).
    """
    targets = [
        device
        for device in (enumerate_devices() or [])
        if device.get("vendor_id") in (0x004C, 0x05AC)
        and device.get("product_id") == product_id
        and device.get("path") is not None
    ]
    if not targets:
        return BatteryResult("not_detected")

    input_candidates = [
        device for device in targets
        if device.get("usage_page") == 0xFF00 and device.get("usage") == 0x14
    ]
    if not input_candidates and (
        request_feature_report is None or feature_report_info is None
    ):
        return BatteryResult("not_detected")

    had_query_failure = False
    had_malformed_report = False
    for device in input_candidates:
        try:
            report = request_report(device["path"])
        except Exception:
            had_query_failure = True
            continue
        try:
            return parse_apple_battery_report(report)
        except (TypeError, ValueError):
            had_malformed_report = True

    if request_feature_report is not None and feature_report_info is not None:
        # Prefer a normal mouse/pointer collection, then inspect other HID
        # collections for this exact Apple VID/PID. No fixed report ID.
        feature_candidates = sorted(
            targets,
            key=lambda d: 0 if (
                d.get("usage_page") == 0x0001 and d.get("usage") == 0x0002
            ) else 1,
        )
        seen_paths = set()
        for device in feature_candidates:
            path = device["path"]
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                info = feature_report_info(path)
            except Exception:
                had_query_failure = True
                continue
            if (
                not isinstance(info, (tuple, list))
                or len(info) != 2
                or type(info[0]) is not int
                or type(info[1]) is not int
                or info[0] < 0
                or info[1] < 2
            ):
                continue

            report_id, report_length = info
            try:
                feature_report = request_feature_report(
                    path, report_id, report_length
                )
            except Exception:
                had_query_failure = True
                continue
            try:
                return parse_apple_battery_feature_report(
                    feature_report, report_id
                )
            except (TypeError, ValueError):
                had_malformed_report = True

    if had_query_failure:
        return BatteryResult("unavailable")
    if had_malformed_report or targets:
        return BatteryResult("unsupported")
    return BatteryResult("not_detected")


def parse_apple_mouse_input_report(report):
    """Parse Magic Mouse v2 Input 0x90; reject a phantom zero-percent response."""
    raw = bytes(report)
    if len(raw) < 3 or raw[0] != 0x90:
        raise ValueError("unexpected Magic Mouse input report")
    if not 1 <= raw[2] <= 100:
        raise ValueError("Magic Mouse battery percentage is outside 1..100")
    return BatteryResult(
        "available",
        percentage=raw[2],
        charging=bool(raw[1] & 0x02),
        fully_charged=bool(raw[1] & 0x04),
    )


def parse_apple_battery_feature_report(report, expected_report_id):
    """Parse descriptor-selected Generic Device Battery Strength Feature data."""
    raw = bytes(report)
    if len(raw) < 2 or raw[0] != expected_report_id:
        raise ValueError("unexpected battery feature report ID")
    if not 1 <= raw[1] <= 100:
        raise ValueError("battery feature percentage is outside 1..100")
    # Feature report exposes percentage only; do not infer charge state.
    return BatteryResult("available", percentage=raw[1])


def parse_apple_mouse_feature_report(report, expected_report_id):
    """Compatibility wrapper for Magic Mouse-specific tests/callers."""
    return parse_apple_battery_feature_report(report, expected_report_id)


def query_apple_mouse_battery(
    enumerate_devices,
    request_input_report,
    request_feature_report=None,
    feature_report_info=None,
):
    """Read Magic Mouse v2 battery without changing or binding any driver.

    Prefer Input 0x90 on its known vendor battery collection. If this is not
    exposed, read Feature only when the same HID collection's descriptor
    advertises Generic Device / Battery Strength (0x0006/0x0020). The report ID
    and length come from the descriptor, never a guessed IOCTL or write.
    """
    targets = [
        device
        for device in (enumerate_devices() or [])
        if device.get("vendor_id") in (0x004C, 0x05AC)
        and device.get("product_id") == 0x0269
        and device.get("path") is not None
    ]
    if not targets:
        return BatteryResult("not_detected")

    vendor_battery = [
        device for device in targets
        if device.get("usage_page") == 0xFF00 and device.get("usage") == 0x14
    ]
    pointer_collections = [
        device for device in targets
        if device.get("usage_page") == 0x0001 and device.get("usage") == 0x0002
    ]

    had_failure = False
    had_invalid = False
    for device in vendor_battery:
        try:
            report = request_input_report(device["path"])
        except Exception:
            had_failure = True
            continue
        try:
            return parse_apple_mouse_input_report(report)
        except (TypeError, ValueError):
            had_invalid = True

    if request_feature_report is not None and feature_report_info is not None:
        # Inspect the ordinary mouse collection first, then other collections
        # of this exact PID. Only a descriptor-declared battery FEATURE is read.
        feature_candidates = pointer_collections + [
            d for d in targets if d not in pointer_collections
        ]
        seen_paths = set()
        for device in feature_candidates:
            path = device["path"]
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                info = feature_report_info(path)
            except Exception:
                had_failure = True
                continue
            if (
                not isinstance(info, (tuple, list))
                or len(info) != 2
                or type(info[0]) is not int
                or type(info[1]) is not int
                or info[0] < 0
                or info[1] < 2
            ):
                continue

            report_id, report_length = info
            try:
                report = request_feature_report(path, report_id, report_length)
            except Exception:
                had_failure = True
                continue
            try:
                return parse_apple_mouse_feature_report(report, report_id)
            except (TypeError, ValueError):
                had_invalid = True

    if had_failure:
        return BatteryResult("unavailable")
    if had_invalid or targets:
        return BatteryResult("unsupported")
    return BatteryResult("not_detected")


def format_battery_label(device_name, result):
    """Return concise dynamic tray text for every battery state."""
    prefix = f"{device_name} battery: "
    if result.status == "available":
        suffix = ""
        if result.fully_charged:
            suffix = " (fully charged)"
        elif result.charging:
            suffix = " (charging)"
        return f"{prefix}{result.percentage}%{suffix}"
    labels = {
        "loading": "loading",
        "not_detected": "not detected",
        "unavailable": "unavailable",
        "unsupported": "unsupported report",
    }
    label = prefix + labels.get(result.status, "unavailable")
    if result.stale and result.last_percentage is not None:
        label += f"; last known {result.last_percentage}% (stale)"
    return label


# Public read-only Bluetooth battery interface from the upstream
# MagicTrackpad2ForWindows AmtPtpControlPanel source:
# \\.\AmtPtpControlDeviceUm + IOCTL_GET_BATTERY (function 0x801,
# FILE_DEVICE_UNKNOWN 0x22, METHOD_BUFFERED, FILE_ANY_ACCESS).
def _ctl_code(device_type, function, method, access):
    return (
        (device_type << 16)
        | (access << 14)
        | (function << 2)
        | method
    )


AMT_PTP_IOCTL_GET_BATTERY = _ctl_code(0x00000022, 0x801, 0, 0)


def query_amt_ptp_driver_battery():
    """Read Trackpad 2 battery through the driver's documented GET_BATTERY IOCTL.

    This invokes only the upstream driver's GET_BATTERY request. It does not
    invoke RELOAD_SETTINGS, write HID feature/output reports, or change state.
    If the upstream control device is absent, unsupported, or returns an
    invalid level, report an unavailable result instead of inventing a value.
    """
    if os.name != "nt":
        return BatteryResult("unavailable")

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        device_io_control = kernel32.DeviceIoControl
        device_io_control.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        device_io_control.restype = wintypes.BOOL
    except Exception:
        return BatteryResult("unavailable")

    # Match the control panel's established sharing/desired-access contract.
    # The only request sent from this app is GET_BATTERY, never its settings IOCTL.
    handle = create_file(
        r"\\.\AmtPtpControlDeviceUm",
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        3,  # OPEN_EXISTING
        0,
        None,
    )
    handle_value = handle if isinstance(handle, int) else getattr(handle, "value", None)
    if handle_value is None or handle_value == ctypes.c_void_p(-1).value:
        return BatteryResult("unavailable")

    try:
        output = ctypes.c_uint32(0)
        returned = wintypes.DWORD(0)
        ok = device_io_control(
            handle,
            AMT_PTP_IOCTL_GET_BATTERY,
            None,
            0,
            ctypes.byref(output),
            ctypes.sizeof(output),
            ctypes.byref(returned),
            None,
        )
        if not ok:
            return BatteryResult("unavailable")
        if returned.value < ctypes.sizeof(output) or output.value > 100:
            return BatteryResult("unsupported")
        return BatteryResult("available", percentage=int(output.value))
    finally:
        close_handle(handle)


class BatteryPoller:
    """Slow Apple HID/control-device battery poller with injected I/O seams."""

    KEYBOARD_PID = 0x0267
    TRACKPAD_PID = 0x0265
    MOUSE_PID = 0x0269

    def __init__(
        self,
        backend,
        publish,
        clock=time.monotonic,
        normal_interval=300.0,
        failure_backoff=900.0,
        stale_after=600.0,
        trackpad_driver_battery_query=None,
    ):
        if normal_interval < 180.0:
            raise ValueError("normal battery polling must be multi-minute")
        if failure_backoff <= normal_interval:
            raise ValueError("failure backoff must exceed normal polling")
        self.backend = backend
        self.publish = publish
        self.clock = clock
        self.normal_interval = float(normal_interval)
        self.failure_backoff = float(failure_backoff)
        self.stale_after = float(stale_after)
        self.trackpad_driver_battery_query = trackpad_driver_battery_query
        self._last_valid = {}

    def _with_stale_state(self, device, result, now):
        if result.status == "available":
            self._last_valid[device] = (result.percentage, now)
            return result
        previous = self._last_valid.get(device)
        if previous is None or now - previous[1] < self.stale_after:
            return result
        return BatteryResult(
            status=result.status,
            stale=True,
            last_percentage=previous[0],
        )

    def poll_once(self):
        now = self.clock()
        try:
            devices = list(self.backend.enumerate_devices() or [])
        except Exception:
            keyboard = trackpad = mouse = BatteryResult("unavailable")
        else:
            enumerate_cycle = lambda: devices
            request_feature = getattr(self.backend, "request_feature_report", None)
            feature_info = getattr(self.backend, "feature_report_info", None)
            keyboard = query_apple_battery(
                self.KEYBOARD_PID,
                enumerate_cycle,
                self.backend.request_report,
                request_feature,
                feature_info,
            )
            trackpad = query_apple_battery(
                self.TRACKPAD_PID,
                enumerate_cycle,
                self.backend.request_report,
                request_feature,
                feature_info,
            )
            target_trackpad_present = any(
                device.get("product_id") == self.TRACKPAD_PID
                and (
                    device.get("vendor_id") in (0x004C, 0x05AC)
                    or "vid&0001004c" in str(device.get("path", "")).lower()
                    or "vid&000205ac" in str(device.get("path", "")).lower()
                    or "vid_05ac" in str(device.get("path", "")).lower()
                )
                for device in devices
            )
            if (
                trackpad.status != "available"
                and target_trackpad_present
                and self.trackpad_driver_battery_query is not None
            ):
                try:
                    driver_result = self.trackpad_driver_battery_query()
                except Exception:
                    driver_result = BatteryResult("unavailable")
                if driver_result.status == "available":
                    trackpad = driver_result
            mouse = query_apple_mouse_battery(
                enumerate_cycle,
                self.backend.request_report,
                getattr(self.backend, "request_feature_report", None),
                getattr(self.backend, "feature_report_info", None),
            )
        keyboard = self._with_stale_state("keyboard", keyboard, now)
        trackpad = self._with_stale_state("trackpad", trackpad, now)
        mouse = self._with_stale_state("mouse", mouse, now)
        snapshot = BatterySnapshot(keyboard, trackpad, now, mouse)
        self.publish(snapshot)
        delay = (
            self.normal_interval
            if "available" in (
                keyboard.status, trackpad.status, mouse.status
            )
            else self.failure_backoff
        )
        return snapshot, delay

    def run(self, stop_event):
        """Poll until shutdown; Event.wait makes even long backoff interruptible."""
        while not stop_event.is_set():
            try:
                _, delay = self.poll_once()
            except Exception:
                delay = self.failure_backoff
            if stop_event.wait(delay):
                break


def contacts_are_stable(entries, now, fresh, min_age):
    """Return whether every contact is current and old enough to be real."""
    return (
        bool(entries)
        and all(now - entry[2] <= fresh for entry in entries)
        and all(now - entry[5] >= min_age for entry in entries)
    )


def should_suppress_pointer(contacts, now, fresh, min_age):
    """Suppress only a confirmed three-finger set, never churn ghosts."""
    entries = list(contacts.values())
    return len(entries) == 3 and contacts_are_stable(
        entries, now=now, fresh=fresh, min_age=min_age
    )


def should_suppress_mouse_event(
    message, flags, extra_info, active, injected_flag=0x01, app_tag=0x50454152
):
    """Return whether an untrusted physical pointer event should be swallowed."""
    pointer_messages = {0x0200, 0x020A, 0x020E}
    return (
        bool(active)
        and message in pointer_messages
        and not (flags & injected_flag)
        and extra_info != app_tag
    )


def expire_stale_contacts(contacts, now, ttl):
    """Drop silent contacts so suppression cannot stick after finger lift."""
    expired = sorted(
        contact_id
        for contact_id, entry in contacts.items()
        if now - entry[2] > ttl
    )
    for contact_id in expired:
        del contacts[contact_id]
    return expired


class TouchpadSettingsManager:
    """Manage reversible Precision Touchpad registry values via callbacks."""

    NATIVE_GESTURES = {
        "ThreeFingerSlideEnabled": 1,
        "ThreeFingerTapEnabled": 1,
    }
    CUSTOM_GESTURES = {
        "ThreeFingerSlideEnabled": 0,
        "ThreeFingerTapEnabled": 0,
    }
    RECOMMENDED = {
        "TapsEnabled": 1,
        "TwoFingerTapEnabled": 1,
        "TapAndDrag": 1,
    }
    VALID_MODES = {"swipes", "drag", "off"}

    def __init__(
        self, config, read_value, write_value, delete_value, save, lock=CONFIG_LOCK
    ):
        self.config = config
        self._read_value = read_value
        self._write_value = write_value
        self._delete_value = delete_value
        self._save = save
        self._lock = lock

    def wanted(self, mode, include_recommended=True):
        with self._lock:
            if mode not in self.VALID_MODES:
                raise ValueError(f"invalid three-finger mode: {mode}")
            wanted = dict(
                self.NATIVE_GESTURES if mode == "off" else self.CUSTOM_GESTURES
            )
            if include_recommended:
                wanted.update(self.RECOMMENDED)
                wanted["ScrollDirection"] = (
                    0 if self.config.get("natural_scroll", False) else 1
                )
            return wanted

    def apply(self, mode, include_recommended=True):
        with self._lock:
            return self._apply_locked(mode, include_recommended)

    def _apply_locked(self, mode, include_recommended=True):
        backup = dict(self.config.get("tp_settings_backup") or {})
        plan = []
        try:
            for name, value in self.wanted(mode, include_recommended).items():
                current = self._read_value(name)
                if current == value:
                    continue
                if name not in backup:
                    backup[name] = current
                plan.append((name, value))
        except Exception:
            # A read error is not evidence that a value is absent. Do not
            # create backup entries or mutate the registry from uncertain data.
            self.config["three_finger_mode"] = "off"
            self.config["tp_settings_applied"] = False
            try:
                self._save()
            except Exception:
                pass
            raise

        # Recovery data must reach disk before the first registry mutation.
        # Applied remains false until every requested write has succeeded.
        self.config["tp_settings_backup"] = backup
        self.config["tp_settings_applied"] = False
        try:
            self._save()
        except Exception:
            self.config["three_finger_mode"] = "off"
            try:
                self._save()
            except Exception:
                pass
            raise

        changed = []
        try:
            for name, value in plan:
                self._write_value(name, value)
                changed.append(name)
            self.config["tp_settings_applied"] = True
            self._save()
        except Exception:
            # Keep the complete pre-mutation backup for Restore/retry, but
            # never permit custom gestures after partial registry ownership.
            self.config["three_finger_mode"] = "off"
            self.config["tp_settings_applied"] = False
            try:
                self._save()
            except Exception:
                pass
            raise
        return changed

    def restore(self):
        with self._lock:
            return self._restore_locked()

    def _restore_locked(self):
        backup = dict(self.config.get("tp_settings_backup") or {})
        # Persist fail-open runtime state first. If the process dies midway,
        # replaying the full backup is safe and cannot re-enable custom input.
        self.config["three_finger_mode"] = "off"
        self.config["tp_settings_applied"] = False
        self._save()

        remaining = dict(backup)
        restored = []
        failures = []
        for name, old_value in backup.items():
            try:
                if old_value is None:
                    self._delete_value(name)
                else:
                    self._write_value(name, old_value)
            except Exception as exc:
                failures.append((name, exc))
            else:
                restored.append(name)
                remaining.pop(name, None)

        self.config["tp_settings_backup"] = remaining
        try:
            self._save()
        except Exception:
            # The durable sidecar still contains the full recovery set. Keep
            # the in-memory view equally conservative so a later retry cannot
            # forget originals merely because pruning failed to persist.
            self.config["tp_settings_backup"] = backup
            raise
        if failures:
            names = ", ".join(name for name, _ in failures)
            first_error = failures[0][1]
            first_error.add_note(f"Could not restore: {names}")
            raise first_error
        return restored

    def enforce(self, mode):
        with self._lock:
            if mode == "off" and not self.config.get("tp_settings_applied", False):
                return []
            return self._apply_locked(mode)

    def status_text(self, mode):
        with self._lock:
            if (
                mode == "off"
                and not self.config.get("tp_settings_applied", False)
                and self.config.get("tp_settings_backup")
            ):
                return "restore incomplete"
            if mode == "off" and not self.config.get("tp_settings_applied", False):
                return "original settings"
            wanted = self.wanted(mode)
            if all(self._read_value(name) == value for name, value in wanted.items()):
                return "applied"
            return "needs apply"
