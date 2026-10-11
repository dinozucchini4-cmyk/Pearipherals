"""Pure, testable core helpers for Pearipherals."""

import json
import os
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import dataclass

from magic_mouse_scroll import HID_ACCESS_LOCK


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
    mouse: BatteryResult | None = None


class BatterySnapshotStore:
    """Publish complete immutable battery snapshots across threads."""

    def __init__(self):
        loading = BatteryResult("loading")
        self._snapshot = BatterySnapshot(loading, loading, 0.0, loading)
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
        readings = [("Magic Keyboard", snapshot.keyboard),
                    ("Magic Trackpad", snapshot.trackpad)]
        if snapshot.mouse is not None:
            readings.append(("Magic Mouse", snapshot.mouse))
        for device, result in readings:
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


class HidBatteryBackend:
    """Small injected hidapi adapter with one handle lifetime per query."""

    def __init__(self, hid_module):
        self._hid = hid_module

    def enumerate_devices(self):
        return self._hid.enumerate()

    def request_report(self, path):
        # Share the HID handle window with the live Magic Mouse reader. The
        # reader's input waits are bounded, so a battery query cannot deadlock.
        with HID_ACCESS_LOCK:
            handle = self._hid.device()
            try:
                handle.open_path(path)
                return bytes(handle.get_input_report(0x90, 3))
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


def query_apple_battery(product_id, enumerate_devices, request_report):
    """Query one exact Apple vendor HID collection through injected I/O."""
    candidates = [
        device
        for device in (enumerate_devices() or [])
        if device.get("vendor_id") == 0x004C
        and device.get("product_id") == product_id
        and device.get("usage_page") == 0xFF00
        and device.get("usage") == 0x14
    ]
    if not candidates:
        return BatteryResult("not_detected")

    had_query_failure = False
    had_malformed_report = False
    for device in candidates:
        try:
            report = request_report(device["path"])
        except Exception:
            had_query_failure = True
            continue
        try:
            return parse_apple_battery_report(report)
        except (TypeError, ValueError):
            had_malformed_report = True

    if had_malformed_report and not had_query_failure:
        return BatteryResult("unsupported")
    return BatteryResult("unavailable")


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


class BatteryPoller:
    """Slow three-device battery poller with injected HID and timing seams."""

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
            keyboard = query_apple_battery(
                self.KEYBOARD_PID, enumerate_cycle, self.backend.request_report
            )
            trackpad = query_apple_battery(
                self.TRACKPAD_PID, enumerate_cycle, self.backend.request_report
            )
            mouse = query_apple_battery(
                self.MOUSE_PID, enumerate_cycle, self.backend.request_report
            )
        keyboard = self._with_stale_state("keyboard", keyboard, now)
        trackpad = self._with_stale_state("trackpad", trackpad, now)
        mouse = self._with_stale_state("mouse", mouse, now)
        snapshot = BatterySnapshot(keyboard, trackpad, now, mouse)
        self.publish(snapshot)
        delay = (
            self.normal_interval
            if "available" in (keyboard.status, trackpad.status, mouse.status)
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
