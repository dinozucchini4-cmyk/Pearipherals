"""Own Magic Mouse 2015 scrolling from the HID touch stream.

The reader is user-mode and read-only: it never sends HID output/feature reports,
installs drivers, edits the registry, or changes device-stack bindings.

Protocol reference used for independent implementation:
Linux kernel drivers/hid/hid-magicmouse.c documents report ID 0x12 for
Magic Mouse 2 and its 8-byte contact records. This module was written
independently; it does not copy the kernel driver's implementation.
"""
from __future__ import annotations

import ctypes
import threading
import time


APPLE_VID = 0x004C
MAGIC_MOUSE_2015_PID = 0x0269
MOUSE2_TOUCH_REPORT_ID = 0x12
TOUCH_STATE_MASK = 0xF0
TOUCH_STATE_START = 0x30
TOUCH_STATE_DRAG = 0x40
MOUSE_COLLECTION_MARKER = "&col02"
BATTERY_USAGE_PAGE = 0xFF00
BATTERY_USAGE = 0x0014

# Serializes short HID battery queries with the touch-report reader. The lock
# is held only during one bounded read/report request, never for a gesture.
HID_ACCESS_LOCK = threading.Lock()


def _signed_bits(value: int, width: int) -> int:
    mask = (1 << width) - 1
    value &= mask
    sign = 1 << (width - 1)
    return value - (1 << width) if value & sign else value


def decode_magic_mouse_contacts(report: bytes | bytearray | list[int]):
    """Decode report-0x12 contacts; return None for non-touch/short reports.

    Report 0x12 has an 8-byte short form and an extended form with a 14-byte
    prefix followed by up to fifteen 8-byte contact records. Windows may pad a
    report to the HID collection's maximum input length, so incomplete trailing
    padding is ignored and never treated as a contact.
    """
    data = bytes(report)
    if not data or data[0] != MOUSE2_TOUCH_REPORT_ID:
        return None
    if len(data) == 8:
        return None
    if len(data) < 22:
        return None

    records = min(15, (len(data) - 14) // 8)
    contacts = []
    for index in range(records):
        start = 14 + index * 8
        item = data[start:start + 8]
        if len(item) != 8 or not any(item):
            continue

        # X and Y are signed 12-bit coordinates packed across bytes.
        x_bits = ((item[1] & 0x0F) << 8) | item[0]
        y_bits = (item[2] << 4) | (item[1] >> 4)
        x = _signed_bits(x_bits, 12)
        y = -_signed_bits(y_bits, 12)
        tracking_id = ((item[6] << 2) | (item[5] >> 6)) & 0x0F
        size = item[5] & 0x3F
        state = item[7] & TOUCH_STATE_MASK
        contacts.append({
            "id": tracking_id,
            "x": x,
            "y": y,
            "size": size,
            "state": state,
        })
    return tuple(contacts)


def select_magic_mouse_touch_path(devices):
    """Choose only PID 0269's vendor/touch collection, never its mouse COL01."""
    for device in devices or ():
        if device.get("vendor_id") != APPLE_VID:
            continue
        if device.get("product_id") != MAGIC_MOUSE_2015_PID:
            continue
        path = device.get("path")
        if path is None:
            continue
        path_text = path.decode(errors="ignore") if isinstance(path, bytes) else str(path)
        lowered = path_text.lower()
        usage_page = device.get("usage_page")
        usage = device.get("usage")
        if MOUSE_COLLECTION_MARKER in lowered or (
            usage_page == BATTERY_USAGE_PAGE and usage == BATTERY_USAGE
        ):
            return path
    return None


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


class MouseWheelInjector:
    """Inject conventional wheel deltas; no pointer or button injection."""

    INPUT_MOUSE = 0
    MOUSEEVENTF_WHEEL = 0x0800
    MOUSEEVENTF_HWHEEL = 0x01000
    EXTRA_INFO_TAG = 0x4F574D55  # "OWMU"

    def __init__(self):
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("Magic Mouse scrolling is supported only on Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = [
            ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int
        ]
        self._user32.SendInput.restype = ctypes.c_uint

    def __call__(self, vertical_delta: int, horizontal_delta: int):
        events = []
        for delta, flags in (
            (vertical_delta, self.MOUSEEVENTF_WHEEL),
            (horizontal_delta, self.MOUSEEVENTF_HWHEEL),
        ):
            if delta:
                event = _INPUT()
                event.type = self.INPUT_MOUSE
                event.u.mi = _MOUSEINPUT(
                    0, 0, ctypes.c_uint32(delta).value, flags, 0,
                    self.EXTRA_INFO_TAG
                )
                events.append(event)
        if not events:
            return
        array = (_INPUT * len(events))(*events)
        sent = self._user32.SendInput(
            len(events), array, ctypes.sizeof(_INPUT)
        )
        if sent != len(events):
            error = ctypes.get_last_error()
            raise OSError(error, "SendInput did not accept all wheel events")


class MagicMouseScrollEngine:
    """Track one firm touch and convert its deltas to Windows wheel units."""

    def __init__(self, send_wheel, settings, clock=time.monotonic,
                 idle_timeout=0.20):
        self._send_wheel = send_wheel
        self._settings = settings
        self._clock = clock
        self._idle_timeout = float(idle_timeout)
        self._active_id = None
        self._last_xy = None
        self._last_touch_at = None
        self._wheel_x = 0.0
        self._wheel_y = 0.0

    def reset(self):
        self._active_id = None
        self._last_xy = None
        self._last_touch_at = None
        self._wheel_x = 0.0
        self._wheel_y = 0.0

    def _expire(self, now):
        if (self._last_touch_at is not None
                and now - self._last_touch_at > self._idle_timeout):
            self.reset()

    def process(self, report, now=None):
        now = self._clock() if now is None else float(now)
        if report is None:
            self._expire(now)
            return
        contacts = decode_magic_mouse_contacts(report)
        if contacts is None:
            self._expire(now)
            return
        self._last_touch_at = now

        # Ignore weak/near-field detections. A single firm contact is required
        # for this first scrolling implementation; any ambiguous multi-touch
        # state releases the scroll baseline rather than guessing.
        active = [contact for contact in contacts
                  if contact["state"] in (TOUCH_STATE_START, TOUCH_STATE_DRAG)
                  and contact["size"] >= 8]
        if len(active) != 1:
            self._active_id = None
            self._last_xy = None
            return

        contact = active[0]
        contact_id = contact["id"]
        x, y = contact["x"], contact["y"]
        if (contact["state"] == TOUCH_STATE_START
                or self._active_id != contact_id or self._last_xy is None):
            self._active_id = contact_id
            self._last_xy = (x, y)
            return

        previous_x, previous_y = self._last_xy
        self._last_xy = (x, y)
        settings = self._settings()
        if not settings.get("enabled", True):
            self._wheel_x = self._wheel_y = 0.0
            return

        speed = settings.get("speed", 32)
        if type(speed) is not int:
            speed = 32
        speed = min(63, max(0, speed))
        scale = max(1.0, float(64 - speed) * 7.0)
        # Screen wheel conventions follow the standard HID driver path:
        # finger-up gives a positive vertical wheel delta.
        dx = x - previous_x
        dy = previous_y - y
        if settings.get("natural", False):
            dx, dy = -dx, -dy
        self._wheel_x += dx * 120.0 / scale
        self._wheel_y += dy * 120.0 / scale

        # Windows understands high-resolution wheel messages as deltas smaller
        # than one 120-unit detent. Legacy apps naturally accumulate these.
        granularity = 12 if settings.get("smooth", True) else 120
        horizontal = int(self._wheel_x / granularity) * granularity
        vertical = int(self._wheel_y / granularity) * granularity
        self._wheel_x -= horizontal
        self._wheel_y -= vertical
        if vertical or horizontal:
            self._send_wheel(vertical, horizontal)


class MagicMouseScrollService:
    """Bounded/reconnecting HID reader. It performs input reads only."""

    def __init__(self, enabled, natural, speed, log, status_changed=None,
                 hid_module=None, injector=None, smooth=None):
        self._enabled = enabled
        self._natural = natural
        self._speed = speed
        self._smooth = smooth or (lambda: True)
        self._log = log
        self._status_changed = status_changed or (lambda _status: None)
        self._hid_module = hid_module
        self._injector = injector
        self._stop = threading.Event()
        self._thread = None
        self._state_lock = threading.Lock()
        self._state = "not started"

    @property
    def state(self):
        with self._state_lock:
            return self._state

    def _set_state(self, state):
        with self._state_lock:
            changed = self._state != state
            self._state = state
        if changed:
            try:
                self._status_changed(state)
            except Exception:
                pass

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._set_state("starting")
        self._thread = threading.Thread(
            target=self._run, name="OwnMagicMouseScroll", daemon=True
        )
        self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        if thread is None or not thread.is_alive():
            self._set_state("stopped")
            return True
        try:
            self._log("Magic Mouse scroll reader did not stop before timeout")
        except Exception:
            pass
        return False

    def _run(self):
        try:
            hid_module = self._hid_module
            if hid_module is None:
                import hid as hid_module
            injector = self._injector or MouseWheelInjector()
        except Exception as exc:
            self._set_state("unavailable")
            try:
                self._log(f"Magic Mouse scroll initialization failed: {exc}")
            except Exception:
                pass
            return

        engine = MagicMouseScrollEngine(
            injector,
            lambda: {
                "enabled": bool(self._enabled()),
                "natural": bool(self._natural()),
                "smooth": bool(self._smooth()),
                "speed": self._speed(),
            },
        )
        last_error = None

        while not self._stop.is_set():
            device = None
            try:
                path = select_magic_mouse_touch_path(
                    hid_module.enumerate(APPLE_VID, MAGIC_MOUSE_2015_PID)
                )
                if path is None:
                    self._set_state("device not detected")
                    self._stop.wait(3.0)
                    continue

                device = hid_module.device()
                device.open_path(path)
                self._set_state("listening for touch reports")
                last_error = None
                while not self._stop.is_set():
                    with HID_ACCESS_LOCK:
                        raw = device.read(256, timeout_ms=250)
                    engine.process(raw)
            except Exception as exc:
                engine.reset()
                message = f"Magic Mouse scroll reader: {exc}"
                if message != last_error:
                    try:
                        self._log(message)
                    except Exception:
                        pass
                    last_error = message
                self._set_state("reconnecting")
                self._stop.wait(2.0)
            finally:
                if device is not None:
                    try:
                        device.close()
                    except Exception:
                        pass
