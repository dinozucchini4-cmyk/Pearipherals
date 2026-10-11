"""Battery alert policy and actual tray wiring, without hardware or a live app."""

import ast
from pathlib import Path
import threading
import time
import types
import unittest
from unittest import mock

import pearipherals_core as core


APP_PATH = Path(__file__).resolve().parents[1] / "pearipherals.py"


def snapshot(keyboard=80, trackpad=80, captured_at=100.0, mouse=None):
    def reading(value):
        return value if isinstance(value, core.BatteryResult) else core.BatteryResult(
            "available", value
        )
    mouse_reading = reading(mouse) if mouse is not None else None
    return core.BatterySnapshot(
        reading(keyboard), reading(trackpad), captured_at, mouse_reading
    )


def evaluate_and_ack(policy, reading, now):
    alerts = policy.evaluate(reading, now)
    policy.acknowledge()
    return alerts


class FakeIcon:
    _hwnd = 123

    def __init__(self, *args, **kwargs):
        self._message_handlers = {}
        self.notifications = []
        self.updates = []

    def run(self, setup):
        setup(self)

    def update_menu(self):
        self.updates.append(threading.get_ident())

    def notify(self, message, title):
        self.notifications.append((message, title, threading.get_ident()))


def app_harness():
    """Execute trusted production definitions; replace only OS/startup effects."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"), str(APP_PATH))
    names = {
        "WindowsTrayMenuRefreshDispatcher", "main", "tray_setup", "_notify",
        "publish_battery_snapshot", "notify_battery_alerts",
        "stop_battery_worker", "on_quit",
    }
    nodes = [node for node in tree.body if (
        isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ) or (
        isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id.startswith("battery_")
            for target in node.targets
        )
    )]
    posts = []
    namespace = {
        **vars(core),
        "threading": types.SimpleNamespace(Lock=threading.Lock, Event=threading.Event,
                                           Thread=mock.Mock()),
        "time": types.SimpleNamespace(monotonic=lambda: 100.0),
        "pystray": types.SimpleNamespace(Icon=FakeIcon, Menu=mock.Mock(), MenuItem=mock.Mock()),
        "user32": types.SimpleNamespace(PostMessageW=lambda *args: posts.append(args) or 1),
        "CONFIG_RECOVERY_ERROR": None,
        "first_run_setup": lambda: False,
        "autostart_get": lambda: False,
        "config_value": lambda key: "off",
        "shutdown_custom_input": lambda *args, **kwargs: [],
        "actions": mock.Mock(),
        "os": types.SimpleNamespace(_exit=mock.Mock()),
        "brightness": types.SimpleNamespace(last_backend=None),
    }
    for name in (
        "migrate_legacy_autostart", "set_tf_mode", "worker", "hook_thread",
        "make_icon", "suppress_pointer", "three_finger_drag", "start_battery_worker",
        "start_mouse_scroll_service", "stop_mouse_scroll_service",
        "on_toggle", "on_mode_swipes", "on_mode_drag", "on_mode_off", "on_natural_scroll",
        "on_mouse_scroll_toggle", "on_mouse_natural_scroll_toggle", "on_mouse_smooth_scroll_toggle", "set_mouse_scroll_speed",
        "mouse_scroll_status_label", "battery_menu_label", "tp_settings", "on_apply_tp",
        "on_restore_tp", "on_autostart", "on_about", "on_export_diagnostics",
        "on_open_docs", "on_report_problem", "on_prepare_removal", "append_error",
    ):
        namespace[name] = mock.Mock()
    exec(compile(ast.Module(nodes, type_ignores=[]), str(APP_PATH), "exec"), namespace)
    namespace["main"]()
    return namespace, namespace["tray_icon"], posts


class BatteryAlertPolicyTests(unittest.TestCase):
    def test_magic_mouse_low_and_critical_alerts_use_fresh_readings(self):
        policy = core.BatteryAlertPolicy()
        self.assertEqual(
            ("Magic Mouse: 20%",),
            evaluate_and_ack(policy, snapshot(mouse=20), 100.0),
        )
        self.assertEqual(
            ("Magic Mouse: 5% (critical)",),
            evaluate_and_ack(policy, snapshot(mouse=5), 100.0),
        )
        self.assertEqual(
            (),
            evaluate_and_ack(policy, snapshot(mouse=5, captured_at=50.0), 100.0),
        )
        self.assertEqual(
            (),
            evaluate_and_ack(policy, snapshot(mouse=core.BatteryResult("unavailable")), 100.0),
        )

    def test_low_then_critical_are_each_attempted_once_per_device(self):
        policy = core.BatteryAlertPolicy()
        self.assertEqual((), evaluate_and_ack(policy, snapshot(21, 21), 100.0))
        self.assertEqual(("Magic Keyboard: 20%",),
                         evaluate_and_ack(policy, snapshot(20, 21), 100.0))
        self.assertEqual((), evaluate_and_ack(policy, snapshot(19, 21), 100.0))
        self.assertEqual(("Magic Trackpad: 20%",),
                         evaluate_and_ack(policy, snapshot(6, 20), 100.0))
        self.assertEqual(("Magic Keyboard: 5% (critical)",),
                         evaluate_and_ack(policy, snapshot(5, 19), 100.0))
        self.assertEqual((), evaluate_and_ack(policy, snapshot(0, 6), 100.0))
        self.assertEqual(("Magic Trackpad: 5% (critical)",),
                         evaluate_and_ack(policy, snapshot(0, 5), 100.0))
        self.assertEqual((), evaluate_and_ack(policy, snapshot(0, 0), 100.0))


    def test_recovery_rearms_only_recovered_device_at_25_not_jitter_or_disconnect(self):
        policy = core.BatteryAlertPolicy()
        self.assertEqual(2, len(evaluate_and_ack(policy, snapshot(5, 5), 100.0)))
        for value in (6, 20, 21, 24, core.BatteryResult("not_detected"),
                      core.BatteryResult("unavailable", stale=True, last_percentage=80)):
            self.assertEqual((), evaluate_and_ack(policy, snapshot(value, value), 100.0))
            self.assertEqual((), evaluate_and_ack(policy, snapshot(5, 5), 100.0))
        self.assertEqual((), evaluate_and_ack(policy, snapshot(25, 5), 100.0))
        self.assertEqual(("Magic Keyboard: 20%",),
                         evaluate_and_ack(policy, snapshot(20, 5), 100.0))
        self.assertEqual(("Magic Keyboard: 5% (critical)",),
                         evaluate_and_ack(policy, snapshot(5, 5), 100.0))
        self.assertEqual((), evaluate_and_ack(policy, snapshot(5, 100), 100.0))
        self.assertEqual(("Magic Trackpad: 0% (critical)",),
                         evaluate_and_ack(policy, snapshot(5, 0), 100.0))

    def test_charging_suppresses_without_consuming_or_resetting_until_recovered(self):
        for flags in ({"charging": True}, {"fully_charged": True}):
            with self.subTest(flags=flags):
                policy = core.BatteryAlertPolicy()
                charging = lambda percent: core.BatteryResult("available", percent, **flags)
                self.assertEqual((), evaluate_and_ack(policy, snapshot(charging(5), charging(20)), 100.0))
                self.assertEqual(("Magic Keyboard: 5% (critical)", "Magic Trackpad: 20%"),
                                 evaluate_and_ack(policy, snapshot(5, 20), 100.0))
                self.assertEqual((), evaluate_and_ack(policy, snapshot(charging(24), charging(24)), 100.0))
                self.assertEqual((), evaluate_and_ack(policy, snapshot(5, 20), 100.0))
                self.assertEqual((), evaluate_and_ack(policy, snapshot(charging(25), charging(25)), 100.0))
                self.assertEqual(("Magic Keyboard: 20%", "Magic Trackpad: 5% (critical)"),
                                 evaluate_and_ack(policy, snapshot(20, 5), 100.0))

    def test_delayed_future_or_invalid_snapshot_time_never_warns_or_rearms(self):
        for captured_at, now in ((100.0, 700.01), (101.0, 100.0),
                                 (float("nan"), 100.0), (100.0, float("inf")),
                                 (None, 100.0), ("100", 100.0), (True, 100.0)):
            with self.subTest(captured_at=captured_at, now=now):
                policy = core.BatteryAlertPolicy()
                self.assertEqual((), evaluate_and_ack(policy, snapshot(0, 0, captured_at), now))
                self.assertEqual(2, len(evaluate_and_ack(policy, snapshot(0, 0), 100.0)))
                self.assertEqual((), evaluate_and_ack(policy, snapshot(100, 100, captured_at), now))
                self.assertEqual((), evaluate_and_ack(policy, snapshot(0, 0), 100.0))
        self.assertEqual(2, len(core.BatteryAlertPolicy().evaluate(snapshot(0, 0), 700.0)))

    def test_invalid_or_nonfresh_readings_never_warn_or_rearm(self):
        invalid = [
            core.BatteryResult("available", value)
            for value in (-1, 101, None, "0", False, True, 5.0, float("nan"))
        ] + [
            core.BatteryResult(status, 0, stale=stale, last_percentage=0)
            for status in ("loading", "not_detected", "unavailable", "unsupported", "unknown")
            for stale in (False, True)
        ] + [core.BatteryResult("available", 0, stale=True),
             core.BatteryResult("available", 100, stale=True)]
        for reading in invalid:
            with self.subTest(reading=reading):
                policy = core.BatteryAlertPolicy()
                self.assertEqual((), evaluate_and_ack(policy, snapshot(reading, reading), 100.0))
                self.assertEqual(("Magic Keyboard: 20%", "Magic Trackpad: 20%"),
                                 evaluate_and_ack(policy, snapshot(20, 20), 100.0))
                evaluate_and_ack(policy, snapshot(reading, reading), 100.0)
                self.assertEqual((), evaluate_and_ack(policy, snapshot(20, 20), 100.0))


class BatteryAlertPollingTests(unittest.TestCase):
    def test_one_available_device_keeps_normal_cadence(self):
        for available_pid in (0x0267, 0x0265):
            with self.subTest(available_pid=available_pid):
                backend = types.SimpleNamespace(
                    enumerate_devices=lambda: [
                        {"vendor_id": 0x004C, "product_id": available_pid,
                         "usage_page": 0xFF00, "usage": 0x14, "path": b"battery"}
                    ],
                    request_report=lambda path: bytes((0x90, 0, 20)),
                )
                poller = core.BatteryPoller(backend, lambda reading: None)
                self.assertEqual(300.0, poller.poll_once()[1])
                backend.enumerate_devices = lambda: []
                self.assertEqual(900.0, poller.poll_once()[1])


class BatteryAlertWiringTests(unittest.TestCase):
    @staticmethod
    def publish_and_dispatch(app, icon, reading):
        app["publish_battery_snapshot"](reading)
        icon._message_handlers[app["battery_menu_refresh"].message](0, 0)

    def test_notification_exception_preserves_alert_for_next_poll(self):
        app, icon, _ = app_harness()
        original_notify = icon.notify
        icon.notify = mock.Mock(side_effect=OSError("tray unavailable"))
        self.publish_and_dispatch(app, icon, snapshot(20, 20))
        icon.notify.assert_called_once()
        icon.notify = original_notify
        self.publish_and_dispatch(app, icon, snapshot(19, 19))
        self.assertEqual(1, len(icon.notifications))
        for _ in range(5):
            self.publish_and_dispatch(app, icon, snapshot(19, 19))
        self.assertEqual(1, len(icon.notifications))
        icon.notify = mock.Mock(side_effect=OSError("critical delivery failed"))
        self.publish_and_dispatch(app, icon, snapshot(5, 19))
        icon.notify = original_notify
        self.publish_and_dispatch(app, icon, snapshot(4, 19))
        self.assertEqual(2, len(icon.notifications))
        self.assertIn("Magic Keyboard: 4% (critical)", icon.notifications[-1][0])
        self.assertNotIn("Magic Trackpad", icon.notifications[-1][0])

    def test_stop_drops_pending_and_late_publications(self):
        app, icon, posts = app_harness()
        app["publish_battery_snapshot"](snapshot(20, 20))
        self.assertTrue(app["stop_battery_worker"]())
        icon._message_handlers[posts[0][1]](0, 0)
        self.publish_and_dispatch(app, icon, snapshot(5, 5))
        self.assertEqual([], icon.notifications)

    def test_close_during_menu_rebuild_drops_notification_callback(self):
        app, icon, posts = app_harness()
        app["publish_battery_snapshot"](snapshot(20, 20))
        entered, release = threading.Event(), threading.Event()

        def rebuild():
            entered.set()
            if not release.wait(1.0):
                raise RuntimeError("test timeout")

        icon.update_menu = rebuild
        tray_thread = threading.Thread(target=lambda: icon._message_handlers[posts[0][1]](0, 0))
        tray_thread.start()
        try:
            self.assertTrue(entered.wait(1.0))
            app["battery_menu_refresh"].close()
        finally:
            release.set()
            tray_thread.join(1.0)
        self.assertFalse(tray_thread.is_alive())
        self.assertEqual([], icon.notifications)
        self.publish_and_dispatch(app, icon, snapshot(5, 5))
        self.assertEqual([], icon.notifications)
        self.assertEqual(1, len(posts))

    def test_startup_messages_precede_battery_worker_so_they_cannot_replace_alerts(self):
        for recovery in (None, RuntimeError("bad config")):
            with self.subTest(recovery=recovery):
                app, icon, _ = app_harness()
                events = []
                app["CONFIG_RECOVERY_ERROR"] = recovery
                app["start_battery_worker"] = lambda: events.append("worker")
                icon.notify = lambda *args: events.append("notify")
                with mock.patch("time.sleep", return_value=None):
                    app["tray_setup"](icon, True)
                self.assertEqual(["notify", "worker"], events)

    def test_quit_during_startup_delay_prevents_late_notification_or_worker(self):
        app, icon, _ = app_harness()
        app["start_battery_worker"].reset_mock()

        def close_during_delay(seconds):
            app["battery_menu_refresh"].close()
            app["stop_battery_worker"]()

        with mock.patch("time.sleep", side_effect=close_during_delay):
            app["tray_setup"](icon, True)
        self.assertEqual([], icon.notifications)
        app["start_battery_worker"].assert_not_called()

    def test_real_poller_to_tray_handles_offline_reconnect_charging_and_recovery(self):
        app, icon, _ = app_harness()
        reports = {}
        now = [100.0]
        devices = {b"keyboard": 0x0267, b"trackpad": 0x0265}
        backend = types.SimpleNamespace(
            enumerate_devices=lambda: [
                {"vendor_id": 0x004C, "product_id": devices[path],
                 "usage_page": 0xFF00, "usage": 0x14, "path": path}
                for path in reports
            ],
            request_report=lambda path: reports[path],
        )
        app["time"].monotonic = lambda: now[0]
        poller = core.BatteryPoller(backend, app["publish_battery_snapshot"], clock=lambda: now[0])
        report = lambda percent, flags=0: bytes((0x90, flags, percent))
        cases = (
            ({}, 0, None),
            ({b"keyboard": report(20), b"trackpad": report(20)}, 1, "Magic Trackpad: 20%"),
            ({}, 1, None),
            ({b"keyboard": report(19), b"trackpad": report(19)}, 1, None),
            ({b"keyboard": report(5), b"trackpad": report(6)}, 2, "Magic Keyboard: 5% (critical)"),
            ({b"keyboard": report(25, 2), b"trackpad": report(24, 2)}, 2, None),
            ({b"keyboard": report(20), b"trackpad": report(5)}, 3, "Magic Trackpad: 5% (critical)"),
            ({b"keyboard": b"", b"trackpad": report(255)}, 3, None),
            ({b"keyboard": report(0), b"trackpad": report(0)}, 4, "Magic Keyboard: 0% (critical)"),
            ({b"keyboard": report(25), b"trackpad": report(25)}, 4, None),
            ({b"keyboard": report(20), b"trackpad": report(20)}, 5, "Magic Trackpad: 20%"),
        )
        for values, count, message in cases:
            with self.subTest(values=values):
                reports.clear()
                reports.update(values)
                now[0] += 300.0
                poller.poll_once()
                icon._message_handlers[app["battery_menu_refresh"].message](0, 0)
                self.assertEqual(count, len(icon.notifications))
                if message:
                    self.assertIn(message, icon.notifications[-1][0])

    def test_worker_burst_coalesces_and_uses_latest_valid_snapshot(self):
        app, icon, posts = app_harness()

        def burst():
            for value in range(30, 9, -1):
                app["publish_battery_snapshot"](snapshot(value, value))

        worker = threading.Thread(target=burst)
        worker.start()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], icon.notifications)
        self.assertEqual(1, len(posts))
        icon._message_handlers[posts[0][1]](0, 0)
        self.assertEqual(1, len(icon.notifications))
        self.assertIn("Magic Keyboard: 10%", icon.notifications[0][0])
        self.assertIn("Magic Trackpad: 10%", icon.notifications[0][0])
        self.publish_and_dispatch(app, icon, snapshot(10, 10))
        self.assertEqual(1, len(icon.notifications))

    def test_failed_post_retries_later_without_losing_initial_alert(self):
        app, icon, posts = app_harness()
        dispatcher = app["battery_menu_refresh"]
        post = dispatcher._post_message
        for outcome in (False, OSError("post failed")):
            with self.subTest(outcome=outcome):
                dispatcher._post_message = mock.Mock(
                    **({"side_effect": outcome} if isinstance(outcome, Exception)
                       else {"return_value": outcome})
                )
                app["publish_battery_snapshot"](snapshot(20, 20))
                self.assertEqual([], icon.notifications)
                self.assertEqual([], posts)
        dispatcher._post_message = post
        self.publish_and_dispatch(app, icon, snapshot(20, 20))
        self.assertEqual(1, len(posts))
        self.assertEqual(1, len(icon.notifications))

    def test_menu_rebuild_failure_does_not_prevent_alert(self):
        app, icon, _ = app_harness()
        icon.update_menu = mock.Mock(side_effect=OSError("menu unavailable"))
        self.publish_and_dispatch(app, icon, snapshot(20, 20))
        self.assertEqual(1, len(icon.notifications))

    def test_tray_delay_and_superseding_offline_or_invalid_reading_suppress_alert(self):
        for newer, now in ((snapshot(0, 0), 701.0),
                           (snapshot(core.BatteryResult("unavailable"),
                                     core.BatteryResult("not_detected")), 100.0),
                           (snapshot(None, "0"), 100.0)):
            with self.subTest(newer=newer):
                app, icon, _ = app_harness()
                app["publish_battery_snapshot"](snapshot(20, 20))
                app["time"].monotonic = lambda: now
                self.publish_and_dispatch(app, icon, newer)
                self.assertEqual([], icon.notifications)
                self.publish_and_dispatch(app, icon, snapshot(0, 0, now))
                self.assertEqual(1, len(icon.notifications))
                self.assertIn("Magic Trackpad: 0% (critical)", icon.notifications[0][0])

    def test_quit_closes_dispatcher_before_stopping_worker_and_drops_late_callback(self):
        app, icon, posts = app_harness()
        app["publish_battery_snapshot"](snapshot(20, 20))
        icon.stop = mock.Mock()
        stop_worker = app["stop_battery_worker"]

        def stop_after_close():
            self.assertFalse(app["battery_menu_refresh"].schedule())
            icon._message_handlers[posts[0][1]](0, 0)
            self.assertEqual([], icon.updates)
            return stop_worker()

        app["stop_battery_worker"] = stop_after_close
        app["on_quit"](icon, None)
        self.publish_and_dispatch(app, icon, snapshot(5, 5))
        self.assertEqual([], icon.notifications)
        self.assertEqual(1, len(posts))
        self.assertTrue(app["battery_stop"].is_set())
        icon.stop.assert_called_once()
        app["os"]._exit.assert_called_once_with(0)

    def test_initial_low_both_devices_notify_once_on_tray_not_worker(self):
        app, icon, posts = app_harness()
        worker = threading.Thread(target=lambda: app["publish_battery_snapshot"](snapshot(20, 19)))
        worker.start()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], icon.notifications)
        self.assertEqual([], icon.updates)
        self.assertEqual(1, len(posts))
        icon._message_handlers[posts[0][1]](0, 0)
        self.assertEqual(1, len(icon.notifications))
        message, title, thread_id = icon.notifications[0]
        self.assertIn("Magic Keyboard: 20%", message)
        self.assertIn("Magic Trackpad: 19%", message)
        self.assertIn("low battery", title.lower())
        self.assertEqual(threading.get_ident(), thread_id)
        self.assertEqual([thread_id], icon.updates)


if __name__ == "__main__":
    unittest.main()
