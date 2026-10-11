"""Protocol parsing and scroll state tests; no hardware or Windows APIs required."""

import unittest

from pearipherals_core import BatteryPoller
from magic_mouse_scroll import (
    MAGIC_MOUSE_2015_PID,
    APPLE_VID,
    MOUSE2_TOUCH_REPORT_ID,
    TOUCH_STATE_DRAG,
    TOUCH_STATE_START,
    MagicMouseScrollEngine,
    decode_magic_mouse_contacts,
    select_magic_mouse_touch_path,
)


def contact_report(*contacts):
    """Create a report 0x12 payload using the documented 8-byte contact layout."""
    report = bytearray(14 + 8 * len(contacts))
    report[0] = MOUSE2_TOUCH_REPORT_ID
    for index, (contact_id, x, y, size, state) in enumerate(contacts):
        x_bits = x & 0xFFF
        y_bits = (-y) & 0xFFF  # the device's Y coordinate is negated by decoder
        rec = 14 + index * 8
        report[rec + 0] = x_bits & 0xFF
        report[rec + 1] = ((x_bits >> 8) & 0x0F) | ((y_bits & 0x0F) << 4)
        report[rec + 2] = (y_bits >> 4) & 0xFF
        report[rec + 5] = (size & 0x3F) | ((contact_id & 0x03) << 6)
        report[rec + 6] = (contact_id >> 2) & 0x03
        report[rec + 7] = state
    return bytes(report)


class MagicMouseReportTests(unittest.TestCase):
    def test_report_0x12_decodes_signed_coordinates_id_size_and_state(self):
        report = contact_report((11, -21, 123, 9, TOUCH_STATE_DRAG))
        self.assertEqual(
            ({"id": 11, "x": -21, "y": 123, "size": 9, "state": TOUCH_STATE_DRAG},),
            decode_magic_mouse_contacts(report),
        )

    def test_eight_byte_non_touch_report_is_not_a_contact_frame(self):
        self.assertIsNone(decode_magic_mouse_contacts(bytes([0x12]) + bytes(7)))

    def test_unknown_report_and_incomplete_report_are_ignored(self):
        self.assertIsNone(decode_magic_mouse_contacts(bytes([0x02]) + bytes(30)))
        self.assertIsNone(decode_magic_mouse_contacts(bytes([0x12]) + bytes(20)))

    def test_multiple_contact_records_are_decoded(self):
        report = contact_report(
            (1, 40, -50, 10, TOUCH_STATE_DRAG),
            (7, -80, 90, 11, TOUCH_STATE_DRAG),
        )
        contacts = decode_magic_mouse_contacts(report)
        self.assertEqual(2, len(contacts))
        self.assertEqual((1, 7), tuple(item["id"] for item in contacts))

    def test_path_selection_is_limited_to_pid_0269_touch_or_vendor_collection(self):
        devices = [
            {"vendor_id": APPLE_VID, "product_id": 0x0269,
             "path": r"\\?\hid#vid_004c&pid_0269&col01#mouse",
             "usage_page": 0x01, "usage": 0x02},
            {"vendor_id": APPLE_VID, "product_id": 0x0323,
             "path": r"\\?\hid#vid_004c&pid_0323&col02#new-mouse",
             "usage_page": 0xFF00, "usage": 0x14},
        ]
        self.assertIsNone(select_magic_mouse_touch_path(devices))
        devices.append(
            {"vendor_id": APPLE_VID, "product_id": MAGIC_MOUSE_2015_PID,
             "path": r"\\?\hid#vid_004c&pid_0269&col02#touch",
             "usage_page": 0xFF00, "usage": 0x14}
        )
        self.assertEqual(devices[-1]["path"], select_magic_mouse_touch_path(devices))


class MagicMouseBatteryIntegrationTests(unittest.TestCase):
    def test_battery_poller_queries_mouse_0269_alongside_keyboard_and_trackpad(self):
        devices = [
            {"vendor_id": 0x004C, "product_id": 0x0267,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"keyboard"},
            {"vendor_id": 0x004C, "product_id": 0x0265,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"trackpad"},
            {"vendor_id": 0x004C, "product_id": 0x0269,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"mouse"},
        ]
        reports = {
            b"keyboard": bytes((0x90, 0x00, 80)),
            b"trackpad": bytes((0x90, 0x00, 92)),
            b"mouse": bytes((0x90, 0x00, 69)),
        }
        class Backend:
            def enumerate_devices(self):
                return list(devices)

            def request_report(self, path):
                return reports[path]

        published = []
        snapshot, _delay = BatteryPoller(
            Backend(), published.append, clock=lambda: 10.0
        ).poll_once()
        self.assertEqual(80, snapshot.keyboard.percentage)
        self.assertEqual(92, snapshot.trackpad.percentage)
        self.assertIsNotNone(snapshot.mouse)
        self.assertEqual(69, snapshot.mouse.percentage)
        self.assertIs(snapshot, published[0])


class MagicMouseScrollEngineTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.settings = {
            "enabled": True,
            "natural": False,
            "smooth": True,
            "speed": 32,
        }
        self.engine = MagicMouseScrollEngine(
            lambda vertical, horizontal: self.events.append((vertical, horizontal)),
            lambda: dict(self.settings),
            clock=lambda: 100.0,
        )

    def test_start_report_anchors_and_drag_generates_smooth_wheel_delta(self):
        self.engine.process(contact_report((1, 0, 0, 10, TOUCH_STATE_START)), now=1.0)
        self.assertEqual([], self.events)
        self.engine.process(contact_report((1, 0, -25, 10, TOUCH_STATE_DRAG)), now=1.02)
        self.assertEqual([(12, 0)], self.events)

    def test_natural_scroll_reverses_wheel_direction(self):
        self.settings["natural"] = True
        self.engine.process(contact_report((1, 0, 0, 10, TOUCH_STATE_START)), now=1.0)
        self.engine.process(contact_report((1, 0, -25, 10, TOUCH_STATE_DRAG)), now=1.02)
        self.assertEqual([(-12, 0)], self.events)

    def test_horizontal_motion_emits_horizontal_wheel(self):
        self.engine.process(contact_report((1, 0, 0, 10, TOUCH_STATE_START)), now=1.0)
        self.engine.process(contact_report((1, 25, 0, 10, TOUCH_STATE_DRAG)), now=1.02)
        self.assertEqual([(0, 12)], self.events)

    def test_disabled_scroll_emits_nothing(self):
        self.settings["enabled"] = False
        self.engine.process(contact_report((1, 0, 0, 10, TOUCH_STATE_START)), now=1.0)
        self.engine.process(contact_report((1, 0, -500, 10, TOUCH_STATE_DRAG)), now=1.02)
        self.assertEqual([], self.events)

    def test_weak_contacts_are_ignored(self):
        self.engine.process(contact_report((1, 0, 0, 5, TOUCH_STATE_START)), now=1.0)
        self.engine.process(contact_report((1, 0, -500, 5, TOUCH_STATE_DRAG)), now=1.02)
        self.assertEqual([], self.events)

    def test_ambiguous_two_finger_motion_resets_baseline_and_does_not_scroll(self):
        self.engine.process(contact_report((1, 0, 0, 10, TOUCH_STATE_START)), now=1.0)
        self.engine.process(
            contact_report(
                (1, 0, -500, 10, TOUCH_STATE_DRAG),
                (2, 20, -500, 12, TOUCH_STATE_DRAG),
            ),
            now=1.02,
        )
        self.assertEqual([], self.events)

    def test_legacy_wheel_mode_uses_full_120_unit_detents(self):
        self.settings["smooth"] = False
        self.engine.process(contact_report((1, 0, 0, 10, TOUCH_STATE_START)), now=1.0)
        self.engine.process(contact_report((1, 0, -224, 10, TOUCH_STATE_DRAG)), now=1.02)
        self.assertEqual([(120, 0)], self.events)


if __name__ == "__main__":
    unittest.main()
