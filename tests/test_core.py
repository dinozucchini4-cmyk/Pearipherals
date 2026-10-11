import ast
import copy
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from pearipherals_core import (
    BatteryPoller,
    BatteryResult,
    BatterySnapshot,
    BatterySnapshotStore,
    HidBatteryBackend,
    CONFIG_LOCK,
    ConfigRecoveryRequired,
    TouchpadSettingsManager,
    expire_stale_contacts,
    format_battery_label,
    is_target_trackpad_path,
    load_json_config,
    parse_apple_battery_report,
    parse_apple_mouse_input_report,
    parse_apple_mouse_feature_report,
    query_apple_battery,
    query_apple_mouse_battery,
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


class InputLifecycleTests(unittest.TestCase):
    @staticmethod
    def _load_app_definition(name, globals_):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        node = next(
            item for item in tree.body
            if isinstance(item, (ast.ClassDef, ast.FunctionDef)) and item.name == name
        )
        namespace = dict(globals_)
        namespace.setdefault("time", time)
        namespace.setdefault("CONFIG_LOCK", CONFIG_LOCK)
        # Definitions that share the support model's fixed wording resolve it at
        # class-definition time, so the single source of truth must be present.
        from pearipherals_support import SUPPORT_ENUMS as _SUPPORT_ENUMS

        namespace.setdefault("SUPPORT_ENUMS", _SUPPORT_ENUMS)
        exec(compile(ast.Module([node], []), source_path, "exec"), namespace)
        return namespace[name]

    def test_raw_input_window_apis_declare_pointer_sized_results(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()

        for declaration in (
            "kernel32.GetModuleHandleW.restype = w.HMODULE",
            "user32.CreateWindowExW.restype = w.HWND",
            "kernel32.CreateMutexW.restype = w.HANDLE",
            "user32.RegisterRawInputDevices.restype = w.BOOL",
        ):
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, source)

        self.assertIn("run_input_callback(\n                    lambda: self._handle_raw_input(lp)", source)
        self.assertIn('"Raw Input maintenance callback"', source)
        self.assertIn("return set_managed_mode(", source)
        self.assertIn('set_tf_mode(config_value("three_finger_mode"))', source)
        self.assertIn("return read_optional_value(query)", source)
        self.assertIn("require_single_input(", source)
        self.assertIn("shutdown_errors = shutdown_custom_input(", source)
        self.assertIn('"Input release failed on Quit"', source)
        self.assertIn("Swipes (Task View / minimize / restore)", source)
        self.assertNotIn("Task View / desktop / switch apps", source)

    def test_release_attempts_pointer_fail_open_even_if_gesture_abort_fails(self):
        events = []

        def abort():
            events.append("abort")
            raise OSError("button release failed")

        errors = release_custom_input(
            abort_gesture=abort,
            disable_suppression=lambda: events.append("disable"),
        )

        self.assertEqual(["abort", "disable"], events)
        self.assertEqual(1, len(errors))

    def test_shutdown_releases_input_before_stopping_suppressor(self):
        events = []

        errors = shutdown_custom_input(
            abort_gesture=lambda: events.append("abort"),
            disable_suppression=lambda: events.append("disable"),
            stop_suppressor=lambda: events.append("stop"),
        )

        self.assertEqual(["abort", "disable", "stop"], events)
        self.assertEqual([], errors)

    def test_failed_left_button_release_remains_visible_and_retryable(self):
        state = {"drag": "dragging"}
        results = iter((0, 1))

        def release():
            require_single_input(lambda: next(results))
            state["drag"] = "idle"

        with self.assertRaises(OSError):
            release()
        self.assertEqual("dragging", state["drag"])

        release()
        self.assertEqual("idle", state["drag"])

    def test_failed_release_does_not_prevent_independent_suppression_cleanup(self):
        state = {"drag": "dragging", "suppression": True}

        def abort():
            require_single_input(lambda: 0)
            state["drag"] = "idle"

        errors = release_custom_input(
            abort,
            lambda: state.__setitem__("suppression", False),
        )

        self.assertEqual("dragging", state["drag"])
        self.assertFalse(state["suppression"])
        self.assertEqual(1, len(errors))

    def test_shutdown_retries_failed_leftup_independently_until_success(self):
        events = []
        attempts = iter((OSError("LEFTUP failed"), None))

        def abort():
            events.append("abort")
            raise next(attempts)

        def retry_leftup():
            events.append("leftup")
            failure = next(attempts)
            if failure:
                raise failure

        errors = shutdown_custom_input(
            abort,
            lambda: events.append("disable"),
            lambda: events.append("stop"),
            retry_release=retry_leftup,
            release_retries=2,
        )

        self.assertEqual(["abort", "disable", "stop", "leftup"], events)
        self.assertEqual([], errors)

    def test_shutdown_bounds_persistently_failed_leftup_retries(self):
        events = []

        def fail(kind):
            events.append(kind)
            raise OSError("LEFTUP failed")

        errors = shutdown_custom_input(
            lambda: fail("abort"),
            lambda: events.append("disable"),
            lambda: events.append("stop"),
            retry_release=lambda: fail("leftup"),
            release_retries=2,
        )

        self.assertEqual(
            ["abort", "disable", "stop", "leftup", "leftup"], events
        )
        self.assertEqual(1, len(errors))
        self.assertIn("LEFTUP failed", str(errors[0]))

    def test_post_thread_message_failure_resets_active_and_allows_retry(self):
        class FakeUser32:
            post_result = 0
            suppressor = None

            def PostThreadMessageW(self, *args):
                if self.post_result:
                    self.suppressor._hook = 99
                    self.suppressor._active = True
                    self.suppressor._operation_generation = args[2]
                    self.suppressor._operation_done.set()
                return self.post_result

        fake_user32 = FakeUser32()
        suppressor_type = self._load_app_definition("PointerSuppressor", {
            "ctypes": ctypes,
            "w": wintypes,
            "threading": threading,
            "user32": fake_user32,
            "kernel32": object(),
            "ULONG_PTR": ctypes.c_size_t,
            "MAGIC_EXTRA": 1,
        })
        suppressor = suppressor_type()
        fake_user32.suppressor = suppressor
        suppressor.start = lambda: (
            setattr(suppressor, "_thread_id", 7),
            setattr(suppressor, "_thread", mock.Mock(is_alive=lambda: True)),
        )

        with self.assertRaises(OSError):
            suppressor.set(True)
        self.assertFalse(suppressor._active)

        fake_user32.post_result = 1
        suppressor.set(True)
        self.assertTrue(suppressor._active)

    def test_hook_install_failure_resets_active_and_allows_retry(self):
        class FakeUser32:
            hook_result = 0

            def SetWindowsHookExW(self, *args):
                return self.hook_result

        fake_user32 = FakeUser32()
        suppressor_type = self._load_app_definition("PointerSuppressor", {
            "ctypes": ctypes,
            "w": wintypes,
            "threading": threading,
            "user32": fake_user32,
            "kernel32": object(),
            "ULONG_PTR": ctypes.c_size_t,
            "MAGIC_EXTRA": 1,
        })
        suppressor = suppressor_type()
        suppressor._proc = object()
        suppressor._active = True
        suppressor._requested_active = True

        self.assertFalse(suppressor._install_hook())
        self.assertFalse(suppressor._active)

        fake_user32.hook_result = 99
        suppressor._requested_active = True
        suppressor._active = True
        self.assertTrue(suppressor._install_hook())
        self.assertTrue(suppressor._active)

    def test_late_successful_install_after_activation_timeout_is_unhooked(self):
        install_started = threading.Event()
        allow_install = threading.Event()
        install_threads = []

        class FakeUser32:
            suppressor = None
            block_install = True
            unhooked = []

            def SetWindowsHookExW(self, *args):
                install_started.set()
                if self.block_install:
                    self.assert_install_released()
                return 99

            @staticmethod
            def assert_install_released():
                if not allow_install.wait(1.0):
                    raise AssertionError("test did not release hook installation")

            def UnhookWindowsHookEx(self, hook):
                self.unhooked.append(hook)
                return 1

            def PostThreadMessageW(self, *args):
                def install():
                    self.suppressor._install_hook()
                    self.suppressor._operation_generation = args[2]
                    self.suppressor._operation_done.set()

                thread = threading.Thread(target=install)
                install_threads.append(thread)
                thread.start()
                return 1

        fake_user32 = FakeUser32()
        suppressor_type = self._load_app_definition("PointerSuppressor", {
            "ctypes": ctypes,
            "w": wintypes,
            "threading": threading,
            "time": time,
            "user32": fake_user32,
            "kernel32": object(),
            "ULONG_PTR": ctypes.c_size_t,
            "MAGIC_EXTRA": 1,
        })
        suppressor = suppressor_type()
        fake_user32.suppressor = suppressor
        suppressor._proc = object()
        suppressor.start = lambda: (
            setattr(suppressor, "_thread_id", 7),
            setattr(suppressor, "_thread", mock.Mock(is_alive=lambda: True)),
        )

        with mock.patch.object(
            suppressor._operation_done,
            "wait",
            side_effect=lambda timeout: install_started.wait(1.0) and False,
        ):
            with self.assertRaisesRegex(OSError, "operation timed out"):
                suppressor.set(True)

        allow_install.set()
        install_threads[0].join(1.0)
        self.assertFalse(install_threads[0].is_alive())
        self.assertEqual([99], fake_user32.unhooked)
        self.assertIsNone(suppressor._hook)
        self.assertFalse(suppressor._active)

        fake_user32.block_install = False
        suppressor.set(True)
        install_threads[1].join(1.0)
        self.assertEqual(99, suppressor._hook)
        self.assertTrue(suppressor._active)

    def test_disable_racing_with_install_never_publishes_stale_hook_active(self):
        install_started = threading.Event()
        allow_install = threading.Event()

        class FakeUser32:
            unhook_results = iter((0, 1))
            unhooked = []

            @staticmethod
            def SetWindowsHookExW(*args):
                install_started.set()
                if not allow_install.wait(1.0):
                    raise AssertionError("test did not release hook installation")
                return 99

            def UnhookWindowsHookEx(self, hook):
                self.unhooked.append(hook)
                return next(self.unhook_results)

        fake_user32 = FakeUser32()
        suppressor_type = self._load_app_definition("PointerSuppressor", {
            "ctypes": ctypes,
            "w": wintypes,
            "threading": threading,
            "time": time,
            "user32": fake_user32,
            "kernel32": object(),
            "ULONG_PTR": ctypes.c_size_t,
            "MAGIC_EXTRA": 1,
        })
        suppressor = suppressor_type()
        suppressor._proc = object()
        suppressor._requested_active = True
        suppressor._request_generation = 4

        installer = threading.Thread(target=suppressor._install_hook, args=(4,))
        installer.start()
        self.assertTrue(install_started.wait(1.0))
        suppressor.set(False)
        allow_install.set()
        installer.join(1.0)

        self.assertFalse(installer.is_alive())
        self.assertEqual([99], fake_user32.unhooked)
        self.assertEqual(99, suppressor._hook)
        self.assertFalse(suppressor._active)

        # The failed immediate cleanup retained the real handle, allowing a
        # later bounded disable to retry and finish removal.
        suppressor.set(False)
        self.assertEqual([99, 99], fake_user32.unhooked)
        self.assertIsNone(suppressor._hook)
        self.assertFalse(suppressor._active)

    def test_start_timeout_fails_closed_and_later_start_can_retry(self):
        suppressor_type = self._load_app_definition("PointerSuppressor", {
            "ctypes": ctypes,
            "w": wintypes,
            "threading": threading,
            "user32": object(),
            "kernel32": object(),
            "ULONG_PTR": ctypes.c_size_t,
            "MAGIC_EXTRA": 1,
        })
        suppressor = suppressor_type()

        class FakeThread:
            def __init__(self, alive, on_start=lambda: None):
                self.alive = alive
                self.on_start = on_start

            def start(self):
                self.on_start()

            def is_alive(self):
                return self.alive

        threads = iter((
            FakeThread(False),
            FakeThread(
                True,
                lambda: (
                    setattr(suppressor, "_thread_id", 17),
                    suppressor._ready.set(),
                ),
            ),
        ))
        with mock.patch.object(
            threading, "Thread", side_effect=lambda **kwargs: next(threads)
        ):
            with self.assertRaises(OSError):
                suppressor.start()
            self.assertFalse(suppressor._active)
            self.assertIsNone(suppressor._thread)

            suppressor.start()
            self.assertEqual(17, suppressor._thread_id)

    def test_live_thread_readiness_timeout_does_not_spawn_duplicate_on_retry(self):
        suppressor_type = self._load_app_definition("PointerSuppressor", {
            "ctypes": ctypes,
            "w": wintypes,
            "threading": threading,
            "user32": object(),
            "kernel32": object(),
            "ULONG_PTR": ctypes.c_size_t,
            "MAGIC_EXTRA": 1,
        })
        suppressor = suppressor_type()
        thread = mock.Mock(is_alive=lambda: True)

        with mock.patch.object(threading, "Thread", return_value=thread) as factory:
            with mock.patch.object(suppressor._ready, "wait", return_value=False):
                with self.assertRaises(OSError):
                    suppressor.start()
                with self.assertRaises(OSError):
                    suppressor.start()

        self.assertEqual(1, factory.call_count)
        self.assertIs(thread, suppressor._thread)
        self.assertFalse(suppressor._active)

        suppressor._thread_id = 19
        suppressor._ready.set()
        suppressor.start()
        self.assertEqual(1, factory.call_count)

    def test_set_true_readiness_failure_is_inactive_and_retryable(self):
        suppressor_type = self._load_app_definition("PointerSuppressor", {
            "ctypes": ctypes,
            "w": wintypes,
            "threading": threading,
            "user32": object(),
            "kernel32": object(),
            "ULONG_PTR": ctypes.c_size_t,
            "MAGIC_EXTRA": 1,
        })
        suppressor = suppressor_type()
        attempts = 0

        def start():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("hook thread did not become ready")
            suppressor._thread_id = 9
            suppressor._thread = mock.Mock(is_alive=lambda: True)

        suppressor.start = start
        suppressor._request_hook_change = lambda active: (
            setattr(suppressor, "_hook", 55),
            setattr(suppressor, "_active", True),
        )

        with self.assertRaises(OSError):
            suppressor.set(True)
        self.assertFalse(suppressor._active)
        self.assertIsNone(suppressor._hook)

        suppressor.set(True)
        self.assertTrue(suppressor._active)
        self.assertEqual(55, suppressor._hook)

    def test_failed_unhook_retains_hook_and_active_state_for_later_retry(self):
        class FakeUser32:
            results = iter((0, 1))

            def UnhookWindowsHookEx(self, hook):
                return next(self.results)

        suppressor_type = self._load_app_definition("PointerSuppressor", {
            "ctypes": ctypes,
            "w": wintypes,
            "threading": threading,
            "user32": FakeUser32(),
            "kernel32": object(),
            "ULONG_PTR": ctypes.c_size_t,
            "MAGIC_EXTRA": 1,
        })
        suppressor = suppressor_type()
        suppressor._hook = 77
        suppressor._active = True

        self.assertFalse(suppressor._uninstall_hook())
        self.assertEqual(77, suppressor._hook)
        self.assertTrue(suppressor._active)

        self.assertTrue(suppressor._uninstall_hook())
        self.assertIsNone(suppressor._hook)
        self.assertFalse(suppressor._active)

    def test_disable_bounds_unhook_retries_and_a_later_disable_can_succeed(self):
        class FakeUser32:
            results = iter((0, 0, 0, 1))
            calls = 0
            suppressor = None

            def UnhookWindowsHookEx(self, hook):
                self.calls += 1
                return next(self.results)

            def PostThreadMessageW(self, *args):
                ok = self.suppressor._uninstall_hook()
                if not ok:
                    self.suppressor._operation_error = OSError("unhook failed")
                self.suppressor._operation_generation = args[2]
                self.suppressor._operation_done.set()
                return 1

        fake_user32 = FakeUser32()
        suppressor_type = self._load_app_definition("PointerSuppressor", {
            "ctypes": ctypes,
            "w": wintypes,
            "threading": threading,
            "user32": fake_user32,
            "kernel32": object(),
            "ULONG_PTR": ctypes.c_size_t,
            "MAGIC_EXTRA": 1,
        })
        suppressor = suppressor_type()
        fake_user32.suppressor = suppressor
        suppressor._hook = 77
        suppressor._active = True
        suppressor._thread_id = 8
        suppressor._thread = mock.Mock(is_alive=lambda: True)

        with self.assertRaises(OSError):
            suppressor.set(False)
        self.assertEqual(3, fake_user32.calls)
        self.assertEqual(77, suppressor._hook)
        self.assertTrue(suppressor._active)

        suppressor.set(False)
        self.assertEqual(4, fake_user32.calls)
        self.assertIsNone(suppressor._hook)
        self.assertFalse(suppressor._active)

    def test_raw_input_callback_logs_failures_and_still_attempts_cleanup(self):
        events = []

        run_input_callback(
            action=lambda: (_ for _ in ()).throw(ValueError("bad report")),
            cleanup=lambda: events.append("cleanup"),
            log=lambda message: events.append(message),
            context="Raw Input callback",
        )

        self.assertEqual("Raw Input callback failed: bad report", events[0])
        self.assertEqual("cleanup", events[1])

    def test_first_run_autostart_failure_blocks_touchpad_mutation_and_success(self):
        app_config = {}
        calls = []

        def failing_autostart(enable):
            calls.append(f"autostart:{enable}")
            raise OSError(r"registry denied at HKCU\...\Run")

        first_run_setup = self._load_app_definition("first_run_setup", {
            "config": app_config,
            "CONFIG_RECOVERY_ERROR": None,
            "CONFIG_LOCK": threading.RLock(),
            "config_value": lambda key, default=None: app_config.get(key, default),
            "autostart_set": failing_autostart,
            "tp_settings": mock.Mock(apply=lambda: calls.append("apply")),
            "save_config": lambda config: calls.append("save"),
            "append_error": lambda message: calls.append(("error", message)),
        })

        # Onboarding stays pending, so the next launch can retry it honestly.
        self.assertFalse(first_run_setup())
        self.assertNotIn("setup_done", app_config)
        # No touchpad registry mutation and no persisted completion.
        self.assertNotIn("apply", calls)
        self.assertNotIn("save", calls)

    def test_first_run_success_toast_claims_no_device_or_driver_detection(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "tray_setup"
        )
        toast = " ".join(
            n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ).lower()

        # It may only state what it actually did.
        self.assertIn("starts with windows", toast)
        self.assertIn("about / status", toast)
        for overclaim in (
            "detected", "found your", "connected", "driver is",
            "magic keyboard is", "magic trackpad is", "ready to use",
        ):
            with self.subTest(overclaim=overclaim):
                self.assertNotIn(overclaim, toast)

    def test_first_run_apply_failure_does_not_mark_setup_done_or_claim_success(self):
        app_config = {}

        class FailedSettings:
            @staticmethod
            def apply():
                raise OSError("registry denied")

        first_run_setup = self._load_app_definition("first_run_setup", {
            "config": app_config,
            "CONFIG_RECOVERY_ERROR": None,
            "CONFIG_LOCK": threading.RLock(),
            "config_value": lambda key, default=None: app_config.get(key, default),
            "autostart_set": lambda enabled: None,
            "tp_settings": FailedSettings(),
            "save_config": lambda config: None,
        })

        self.assertFalse(first_run_setup())
        self.assertNotIn("setup_done", app_config)

    def test_recovery_required_skips_first_run_registry_apply(self):
        app_config = {"three_finger_mode": "off"}
        calls = []
        first_run_setup = self._load_app_definition("first_run_setup", {
            "config": app_config,
            "CONFIG_RECOVERY_ERROR": OSError("invalid config"),
            "CONFIG_LOCK": threading.RLock(),
            "autostart_set": lambda enabled: calls.append("autostart"),
            "tp_settings": mock.Mock(apply=lambda: calls.append("apply")),
            "save_config": lambda config: calls.append("save"),
        })

        self.assertFalse(first_run_setup())
        self.assertEqual([], calls)
        self.assertEqual({"three_finger_mode": "off"}, app_config)


class ConfigPersistenceTests(unittest.TestCase):
    def test_trustworthy_startup_observes_product_version_transactionally(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()

        self.assertRegex(
            source,
            r"from pearipherals_version import \(?[^)]*\bAPP_VERSION\b",
        )
        self.assertRegex(
            source,
            r"from pearipherals_support import \(?[^)]*\bobserve_version\b",
        )
        self.assertIn(
            "observe_version(config, APP_VERSION, lambda: save_config(config))",
            source,
        )

    def test_config_v6_migration_preserves_unknown_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "pearipherals.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"cfg_version": 5, "future_setting": {"keep": True}}, stream)

            source_path = os.path.join(
                os.path.dirname(__file__), "..", "pearipherals.py"
            )
            with open(source_path, "r", encoding="utf-8") as stream:
                tree = ast.parse(stream.read(), source_path)
            defaults_node = next(
                node for node in tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "DEFAULTS"
                    for target in node.targets
                )
            )
            defaults = ast.literal_eval(defaults_node.value)
            saves = []
            load_config = InputLifecycleTests._load_app_definition(
                "load_config",
                {
                    "CONFIG_LOCK": threading.RLock(),
                    "CONFIG_PATH": path,
                    "DEFAULTS": defaults,
                    "OLD_CONFIG_PATHS": (),
                    "load_json_config": load_json_config,
                    "ConfigRecoveryRequired": ConfigRecoveryRequired,
                    "save_config": lambda cfg: saves.append(copy.deepcopy(cfg)),
                },
            )

            migrated = load_config()

        self.assertEqual(6, migrated["cfg_version"])
        self.assertIsNone(migrated["current_version"])
        self.assertIsNone(migrated["previous_version"])
        self.assertIsNone(migrated["legacy_source"])
        self.assertEqual({"keep": True}, migrated["future_setting"])
        self.assertEqual([migrated], saves)

    def test_incompatible_primary_config_versions_require_recovery_without_overwrite(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        defaults = ast.literal_eval(next(
            node.value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "DEFAULTS"
                for target in node.targets
            )
        ))

        for version in (7, "6", True):
            with self.subTest(version=version):
                with tempfile.TemporaryDirectory() as folder:
                    path = os.path.join(folder, "pearipherals.json")
                    original = json.dumps({"cfg_version": version}).encode("utf-8")
                    with open(path, "wb") as stream:
                        stream.write(original)
                    saves = []
                    load_config = InputLifecycleTests._load_app_definition(
                        "load_config",
                        {
                            "CONFIG_LOCK": threading.RLock(),
                            "CONFIG_PATH": path,
                            "DEFAULTS": defaults,
                            "OLD_CONFIG_PATHS": (),
                            "load_json_config": load_json_config,
                            "ConfigRecoveryRequired": ConfigRecoveryRequired,
                            "save_config": lambda cfg: saves.append(dict(cfg)),
                        },
                    )

                    with self.assertRaises(ConfigRecoveryRequired) as raised:
                        load_config()

                    self.assertEqual(path, raised.exception.path)
                    self.assertEqual([], saves)
                    with open(path, "rb") as stream:
                        self.assertEqual(original, stream.read())

    def test_malformed_first_priority_legacy_requires_recovery_without_fallback(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        defaults = ast.literal_eval(next(
            node.value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "DEFAULTS"
                for target in node.targets
            )
        ))

        with tempfile.TemporaryDirectory() as folder:
            primary = os.path.join(folder, "pearipherals.json")
            first = os.path.join(folder, "magicsuite.json")
            second = os.path.join(folder, "magickeys.json")
            malformed = b'{"cfg_version": 5'
            with open(first, "wb") as stream:
                stream.write(malformed)
            with open(second, "w", encoding="utf-8") as stream:
                json.dump({"cfg_version": 6}, stream)
            saves = []
            load_config = InputLifecycleTests._load_app_definition(
                "load_config",
                {
                    "CONFIG_LOCK": threading.RLock(),
                    "CONFIG_PATH": primary,
                    "DEFAULTS": defaults,
                    "OLD_CONFIG_PATHS": (
                        (first, "MagicSuite"),
                        (second, "MagicKeys"),
                    ),
                    "load_json_config": load_json_config,
                    "ConfigRecoveryRequired": ConfigRecoveryRequired,
                    "save_config": lambda cfg: saves.append(dict(cfg)),
                },
            )

            with self.assertRaises(ConfigRecoveryRequired) as raised:
                load_config()

            self.assertEqual(first, raised.exception.path)
            self.assertEqual([], saves)
            self.assertFalse(os.path.exists(primary))
            with open(first, "rb") as stream:
                self.assertEqual(malformed, stream.read())

    def test_incompatible_first_priority_legacy_requires_recovery_without_fallback(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        defaults = ast.literal_eval(next(
            node.value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "DEFAULTS"
                for target in node.targets
            )
        ))

        with tempfile.TemporaryDirectory() as folder:
            primary = os.path.join(folder, "pearipherals.json")
            first = os.path.join(folder, "magicsuite.json")
            second = os.path.join(folder, "magickeys.json")
            original = json.dumps({"cfg_version": 7}).encode("utf-8")
            with open(first, "wb") as stream:
                stream.write(original)
            with open(second, "w", encoding="utf-8") as stream:
                json.dump({"cfg_version": 6}, stream)
            saves = []
            load_config = InputLifecycleTests._load_app_definition(
                "load_config",
                {
                    "CONFIG_LOCK": threading.RLock(),
                    "CONFIG_PATH": primary,
                    "DEFAULTS": defaults,
                    "OLD_CONFIG_PATHS": (
                        (first, "MagicSuite"),
                        (second, "MagicKeys"),
                    ),
                    "load_json_config": load_json_config,
                    "ConfigRecoveryRequired": ConfigRecoveryRequired,
                    "save_config": lambda cfg: saves.append(dict(cfg)),
                },
            )

            with self.assertRaises(ConfigRecoveryRequired) as raised:
                load_config()

            self.assertEqual(first, raised.exception.path)
            self.assertEqual([], saves)
            self.assertFalse(os.path.exists(primary))
            with open(first, "rb") as stream:
                self.assertEqual(original, stream.read())

    def test_legacy_adoption_persists_only_fixed_source_label(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        defaults_node = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "DEFAULTS"
                for target in node.targets
            )
        )
        defaults = ast.literal_eval(defaults_node.value)

        for filename, label in (
            ("magicsuite.json", "MagicSuite"),
            ("magickeys.json", "MagicKeys"),
        ):
            with self.subTest(filename=filename):
                with tempfile.TemporaryDirectory() as folder:
                    primary = os.path.join(folder, "pearipherals.json")
                    legacy = os.path.join(folder, filename)
                    with open(legacy, "w", encoding="utf-8") as stream:
                        json.dump(
                            {"cfg_version": 6, "future_setting": "kept"}, stream
                        )
                    saves = []
                    load_config = InputLifecycleTests._load_app_definition(
                        "load_config",
                        {
                            "CONFIG_LOCK": threading.RLock(),
                            "CONFIG_PATH": primary,
                            "DEFAULTS": defaults,
                            "OLD_CONFIG_PATHS": ((legacy, label),),
                            "load_json_config": load_json_config,
                            "ConfigRecoveryRequired": ConfigRecoveryRequired,
                            "save_config": lambda cfg: saves.append(
                                copy.deepcopy(cfg)
                            ),
                        },
                    )

                    adopted = load_config()

                self.assertEqual(label, adopted["legacy_source"])
                self.assertEqual("kept", adopted["future_setting"])
                self.assertEqual(label, saves[0]["legacy_source"])
                self.assertNotIn(legacy, json.dumps(adopted))

    def test_atomic_save_replaces_json_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            save_json_atomic(path, {"mode": "swipes"})

            with open(path, "r", encoding="utf-8") as stream:
                self.assertEqual({"mode": "swipes"}, json.load(stream))
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_concurrent_saves_produce_valid_json_without_temp_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            payloads = (
                {"writer": 1, "values": list(range(100))},
                {"writer": 2, "values": list(range(100, 200))},
            )
            first_dump_started = threading.Event()
            second_dump_started = threading.Event()
            original_dump = json.dump
            dump_calls = 0
            dump_lock = threading.Lock()
            errors = []

            def coordinated_dump(data, stream, **kwargs):
                nonlocal dump_calls
                with dump_lock:
                    dump_calls += 1
                    call = dump_calls
                if call == 1:
                    first_dump_started.set()
                    second_dump_started.wait(0.2)
                elif call == 2:
                    second_dump_started.set()
                return original_dump(data, stream, **kwargs)

            def save(payload):
                try:
                    save_json_atomic(path, payload)
                except Exception as exc:
                    errors.append(exc)

            with mock.patch("pearipherals_core.json.dump", side_effect=coordinated_dump):
                first = threading.Thread(target=save, args=(payloads[0],))
                second = threading.Thread(target=save, args=(payloads[1],))
                first.start()
                self.assertTrue(first_dump_started.wait(1.0))
                second.start()
                first.join(2.0)
                second.join(2.0)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual([], errors)
            with open(path, "r", encoding="utf-8") as stream:
                self.assertIn(json.load(stream), payloads)
            artifacts = [
                name for name in os.listdir(folder)
                if name != os.path.basename(path)
            ]
            self.assertEqual([], artifacts)

    def test_mutation_of_same_dictionary_blocks_during_serialization(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            data = {"generation": "before", "values": list(range(100))}
            dump_started = threading.Event()
            mutation_attempted = threading.Event()
            mutation_done = threading.Event()
            original_dump = json.dump

            def coordinated_dump(payload, stream, **kwargs):
                dump_started.set()
                self.assertTrue(mutation_attempted.wait(1.0))
                self.assertFalse(mutation_done.wait(0.05))
                return original_dump(payload, stream, **kwargs)

            def mutate():
                self.assertTrue(dump_started.wait(1.0))
                mutation_attempted.set()
                with CONFIG_LOCK:
                    data.clear()
                    data.update({"generation": "after", "values": [999]})
                mutation_done.set()

            with mock.patch(
                "pearipherals_core.json.dump", side_effect=coordinated_dump
            ):
                writer = threading.Thread(
                    target=lambda: save_json_atomic(path, data)
                )
                mutator = threading.Thread(target=mutate)
                writer.start()
                mutator.start()
                writer.join(2.0)
                mutator.join(2.0)

            self.assertFalse(writer.is_alive())
            self.assertFalse(mutator.is_alive())
            with open(path, "r", encoding="utf-8") as stream:
                self.assertEqual(
                    {"generation": "before", "values": list(range(100))},
                    json.load(stream),
                )
            self.assertEqual([os.path.basename(path)], os.listdir(folder))

    def test_malformed_primary_config_is_preserved_and_requires_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "pearipherals.json")
            malformed = b'{"tp_settings_backup":{"TapAndDrag":0}'
            with open(path, "wb") as stream:
                stream.write(malformed)

            with self.assertRaises(ConfigRecoveryRequired) as raised:
                load_json_config(path, {"three_finger_mode": "swipes"})

            self.assertEqual(path, raised.exception.path)
            with open(path, "rb") as stream:
                self.assertEqual(malformed, stream.read())
            self.assertEqual(["pearipherals.json"], os.listdir(folder))

    def test_unreadable_existing_primary_requires_recovery_not_first_run(self):
        path = os.path.join("protected", "pearipherals.json")
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            with self.assertRaises(ConfigRecoveryRequired) as raised:
                load_json_config(path, {"three_finger_mode": "swipes"})

        self.assertEqual(path, raised.exception.path)
        self.assertIsInstance(raised.exception.cause, PermissionError)

    def test_missing_primary_config_is_distinct_from_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "pearipherals.json")

            config, state = load_json_config(
                path, {"three_finger_mode": "swipes"}
            )

            self.assertEqual({"three_finger_mode": "swipes"}, config)
            self.assertEqual("missing", state)

    def test_failed_replace_preserves_previous_valid_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            save_json_atomic(path, {"mode": "off"})

            with mock.patch("pearipherals_core.os.replace", side_effect=OSError("busy")):
                with self.assertRaises(OSError):
                    save_json_atomic(path, {"mode": "swipes"})

            with open(path, "r", encoding="utf-8") as stream:
                self.assertEqual({"mode": "off"}, json.load(stream))
            self.assertFalse(os.path.exists(path + ".tmp"))


class TouchpadSettingsManagerTests(unittest.TestCase):
    def test_custom_apply_write_failure_fails_closed_without_losing_backup(self):
        values = {
            "ThreeFingerSlideEnabled": 1,
            "ThreeFingerTapEnabled": 1,
        }
        config = {"three_finger_mode": "swipes"}
        saves = []

        def write(name, value):
            if name == "ThreeFingerTapEnabled":
                raise OSError("registry denied")
            values[name] = value

        manager = TouchpadSettingsManager(
            config=config,
            read_value=lambda name: values.get(name),
            write_value=write,
            delete_value=lambda name: values.pop(name, None),
            save=lambda: saves.append(dict(config)),
        )

        with self.assertRaises(OSError):
            manager.apply("swipes", include_recommended=False)

        self.assertEqual("off", config["three_finger_mode"])
        self.assertFalse(config["tp_settings_applied"])
        self.assertEqual(
            {"ThreeFingerSlideEnabled": 1, "ThreeFingerTapEnabled": 1},
            config["tp_settings_backup"],
        )
        self.assertFalse(saves[0]["tp_settings_applied"])
        self.assertFalse(saves[-1]["tp_settings_applied"])

    def test_mode_switch_registry_failure_rolls_persisted_mode_back_to_off(self):
        config = {"three_finger_mode": "off", "tp_settings_applied": False}
        saved_modes = []

        with self.assertRaises(OSError):
            set_managed_mode(
                config,
                "drag",
                enforce=lambda mode: (_ for _ in ()).throw(OSError("denied")),
                save=lambda: saved_modes.append(config["three_finger_mode"]),
            )

        self.assertEqual(["drag", "off"], saved_modes)
        self.assertEqual("off", config["three_finger_mode"])
        self.assertFalse(config["tp_settings_applied"])

    def test_startup_enforcement_failure_rolls_existing_custom_mode_off(self):
        config = {"three_finger_mode": "swipes", "tp_settings_applied": True}

        with self.assertRaises(OSError):
            set_managed_mode(
                config,
                config["three_finger_mode"],
                enforce=lambda mode: (_ for _ in ()).throw(OSError("denied")),
                save=lambda: None,
            )

        self.assertEqual("off", config["three_finger_mode"])
        self.assertFalse(config["tp_settings_applied"])

    def test_registry_missing_value_maps_to_absence(self):
        self.assertIsNone(read_optional_value(
            lambda: (_ for _ in ()).throw(FileNotFoundError("missing"))
        ))

    def test_registry_read_error_aborts_apply_before_save_or_mutation(self):
        config = {"three_finger_mode": "swipes"}
        writes = []
        saves = []
        manager = TouchpadSettingsManager(
            config=config,
            read_value=lambda name: read_optional_value(
                lambda: (_ for _ in ()).throw(OSError("registry unavailable"))
            ),
            write_value=lambda name, value: writes.append((name, value)),
            delete_value=lambda name: None,
            save=lambda: saves.append(dict(config)),
        )

        with self.assertRaises(OSError):
            manager.apply("swipes", include_recommended=False)

        self.assertEqual([], writes)
        self.assertEqual("off", config["three_finger_mode"])
        self.assertFalse(config["tp_settings_applied"])
        self.assertEqual(1, len(saves))
        self.assertNotIn("tp_settings_backup", config)

    def test_apply_persists_recovery_backup_before_registry_mutation(self):
        values = {"ThreeFingerSlideEnabled": 1}
        config = {"three_finger_mode": "swipes"}
        events = []

        manager = TouchpadSettingsManager(
            config=config,
            read_value=lambda name: values.get(name),
            write_value=lambda name, value: (
                events.append(("write", name)), values.__setitem__(name, value)
            )[-1],
            delete_value=lambda name: values.pop(name, None),
            save=lambda: events.append(("save", dict(config))),
        )

        manager.apply("swipes", include_recommended=False)

        self.assertEqual("save", events[0][0])
        self.assertEqual(1, events[0][1]["tp_settings_backup"][
            "ThreeFingerSlideEnabled"
        ])

    def test_restore_keeps_failed_backup_entry_for_retry(self):
        values = {"TapsEnabled": 1, "TapAndDrag": 1}
        config = {
            "three_finger_mode": "swipes",
            "tp_settings_applied": True,
            "tp_settings_backup": {"TapsEnabled": 0, "TapAndDrag": 0},
        }

        def write(name, value):
            if name == "TapAndDrag":
                raise OSError("registry busy")
            values[name] = value

        manager = TouchpadSettingsManager(
            config=config,
            read_value=lambda name: values.get(name),
            write_value=write,
            delete_value=lambda name: values.pop(name, None),
            save=lambda: None,
        )

        with self.assertRaises(OSError):
            manager.restore()

        self.assertEqual(0, values["TapsEnabled"])
        self.assertEqual(1, values["TapAndDrag"])
        self.assertEqual({"TapAndDrag": 0}, config["tp_settings_backup"])
        self.assertEqual("off", config["three_finger_mode"])
        self.assertFalse(config["tp_settings_applied"])
        self.assertEqual("restore incomplete", manager.status_text("off"))

    def test_apply_swipes_backs_up_originals_and_disables_native_gestures(self):
        values = {
            "ThreeFingerSlideEnabled": 1,
            "ThreeFingerTapEnabled": 1,
            "TapsEnabled": 0,
            "TwoFingerTapEnabled": 0,
            "TapAndDrag": 0,
            "ScrollDirection": 0,
        }
        config = {"three_finger_mode": "swipes", "natural_scroll": False}
        saves = []

        manager = TouchpadSettingsManager(
            config=config,
            read_value=lambda name: values.get(name),
            write_value=lambda name, value: values.__setitem__(name, value),
            delete_value=lambda name: values.pop(name, None),
            save=lambda: saves.append(dict(config)),
        )

        changed = manager.apply("swipes")

        self.assertEqual(0, values["ThreeFingerSlideEnabled"])
        self.assertEqual(0, values["ThreeFingerTapEnabled"])
        self.assertEqual(1, values["TapsEnabled"])
        self.assertEqual(1, values["TwoFingerTapEnabled"])
        self.assertEqual(1, values["TapAndDrag"])
        self.assertEqual(1, values["ScrollDirection"])
        self.assertEqual(
            {
                "ThreeFingerSlideEnabled": 1,
                "ThreeFingerTapEnabled": 1,
                "TapsEnabled": 0,
                "TwoFingerTapEnabled": 0,
                "TapAndDrag": 0,
                "ScrollDirection": 0,
            },
            config["tp_settings_backup"],
        )
        self.assertTrue(config["tp_settings_applied"])
        self.assertEqual(6, len(changed))
        self.assertEqual(2, len(saves))
        self.assertFalse(saves[0]["tp_settings_applied"])
        self.assertTrue(saves[1]["tp_settings_applied"])

    def test_restore_reinstates_every_backup_and_turns_custom_gestures_off(self):
        values = {
            "ThreeFingerSlideEnabled": 0,
            "ThreeFingerTapEnabled": 0,
            "TapsEnabled": 1,
            "TwoFingerTapEnabled": 1,
        }
        config = {
            "three_finger_mode": "swipes",
            "tp_settings_applied": True,
            "tp_settings_backup": {
                "ThreeFingerSlideEnabled": 1,
                "ThreeFingerTapEnabled": 1,
                "TapsEnabled": 0,
                "TwoFingerTapEnabled": None,
            },
        }
        saves = []

        manager = TouchpadSettingsManager(
            config=config,
            read_value=lambda name: values.get(name),
            write_value=lambda name, value: values.__setitem__(name, value),
            delete_value=lambda name: values.pop(name, None),
            save=lambda: saves.append(dict(config)),
        )

        restored = manager.restore()

        self.assertEqual(1, values["ThreeFingerSlideEnabled"])
        self.assertEqual(1, values["ThreeFingerTapEnabled"])
        self.assertEqual(0, values["TapsEnabled"])
        self.assertNotIn("TwoFingerTapEnabled", values)
        self.assertEqual("off", config["three_finger_mode"])
        self.assertFalse(config["tp_settings_applied"])
        self.assertEqual({}, config["tp_settings_backup"])
        self.assertEqual(4, len(restored))
        self.assertEqual(2, len(saves))

    def test_enforce_off_leaves_restored_registry_values_alone(self):
        values = {
            "ThreeFingerSlideEnabled": 0,
            "ThreeFingerTapEnabled": 0,
        }
        config = {
            "three_finger_mode": "off",
            "tp_settings_applied": False,
            "tp_settings_backup": {},
        }
        writes = []

        manager = TouchpadSettingsManager(
            config=config,
            read_value=lambda name: values.get(name),
            write_value=lambda name, value: writes.append((name, value)),
            delete_value=lambda name: values.pop(name, None),
            save=lambda: None,
        )

        manager.enforce("off")

        self.assertEqual([], writes)
        self.assertEqual("original settings", manager.status_text("off"))

    def test_wanted_apply_status_and_startup_enforcement_share_every_mode_plan(self):
        for mode in ("swipes", "drag", "off"):
            with self.subTest(mode=mode):
                values = {
                    "ThreeFingerSlideEnabled": 9,
                    "ThreeFingerTapEnabled": 9,
                    "TapsEnabled": 0,
                    "TwoFingerTapEnabled": 0,
                    "TapAndDrag": 0,
                    "ScrollDirection": 0,
                }
                config = {
                    "three_finger_mode": mode,
                    "natural_scroll": False,
                    "tp_settings_applied": True,
                }
                manager = TouchpadSettingsManager(
                    config,
                    read_value=lambda name: values.get(name),
                    write_value=lambda name, value: values.__setitem__(name, value),
                    delete_value=lambda name: values.pop(name, None),
                    save=lambda: None,
                )
                wanted = manager.wanted(mode)

                manager.apply(mode)
                self.assertEqual(wanted, {name: values[name] for name in wanted})
                self.assertEqual("applied", manager.status_text(mode))
                self.assertEqual([], manager.enforce(mode))
                self.assertEqual(1, values["ScrollDirection"])
                expected_native = 1 if mode == "off" else 0
                self.assertEqual(expected_native, values["ThreeFingerSlideEnabled"])
                self.assertEqual(expected_native, values["ThreeFingerTapEnabled"])

    def test_every_apply_registry_write_failure_retains_complete_original_plan(self):
        wanted = {
            **TouchpadSettingsManager.CUSTOM_GESTURES,
            **TouchpadSettingsManager.RECOMMENDED,
            "ScrollDirection": 1,
        }
        names = list(wanted)
        for failed_index in range(len(names)):
            with self.subTest(failed_index=failed_index):
                original = {name: 1 - value for name, value in wanted.items()}
                values = dict(original)
                config = {"three_finger_mode": "swipes", "natural_scroll": False}
                writes = 0

                def write(name, value):
                    nonlocal writes
                    if writes == failed_index:
                        raise OSError(f"write {failed_index} failed")
                    writes += 1
                    values[name] = value

                manager = TouchpadSettingsManager(
                    config,
                    read_value=lambda name: values.get(name),
                    write_value=write,
                    delete_value=lambda name: values.pop(name, None),
                    save=lambda: None,
                )
                with self.assertRaises(OSError):
                    manager.apply("swipes")
                self.assertEqual(original, config["tp_settings_backup"])
                self.assertFalse(config["tp_settings_applied"])
                self.assertEqual("off", config["three_finger_mode"])

    def test_apply_final_persistence_failure_keeps_complete_backup_failed_closed(self):
        values = {"ThreeFingerSlideEnabled": 1, "ThreeFingerTapEnabled": 1}
        original = dict(values)
        config = {"three_finger_mode": "swipes"}
        saves = 0

        def save():
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError("success marker save failed")

        manager = TouchpadSettingsManager(
            config,
            lambda name: values.get(name),
            lambda name, value: values.__setitem__(name, value),
            lambda name: values.pop(name, None),
            save,
        )
        with self.assertRaises(OSError):
            manager.apply("swipes", include_recommended=False)
        self.assertEqual(original, config["tp_settings_backup"])
        self.assertEqual("off", config["three_finger_mode"])
        self.assertFalse(config["tp_settings_applied"])

    def test_restore_initial_persistence_failure_performs_no_registry_operation(self):
        config = {
            "three_finger_mode": "swipes",
            "tp_settings_applied": True,
            "tp_settings_backup": {"A": 10, "B": None},
        }
        events = []
        manager = TouchpadSettingsManager(
            config,
            lambda name: 1,
            lambda name, value: events.append(("write", name, value)),
            lambda name: events.append(("delete", name)),
            lambda: (_ for _ in ()).throw(OSError("pre-restore save failed")),
        )
        with self.assertRaises(OSError):
            manager.restore()
        self.assertEqual([], events)
        self.assertEqual({"A": 10, "B": None}, config["tp_settings_backup"])
        self.assertEqual("off", config["three_finger_mode"])
        self.assertFalse(config["tp_settings_applied"])

    def test_every_restore_failure_retains_exact_failed_entries_for_retry(self):
        backup = {"A": 10, "B": None, "C": 30}
        for failed_name in backup:
            with self.subTest(failed_name=failed_name):
                values = {"A": 1, "B": 2, "C": 3}
                config = {
                    "three_finger_mode": "swipes",
                    "tp_settings_applied": True,
                    "tp_settings_backup": dict(backup),
                }

                def write(name, value):
                    if name == failed_name:
                        raise OSError("restore write failed")
                    values[name] = value

                def delete(name):
                    if name == failed_name:
                        raise OSError("restore delete failed")
                    values.pop(name, None)

                manager = TouchpadSettingsManager(
                    config, lambda name: values.get(name), write, delete, lambda: None
                )
                with self.assertRaises(OSError):
                    manager.restore()
                self.assertEqual({failed_name: backup[failed_name]},
                                 config["tp_settings_backup"])
                self.assertEqual("off", config["three_finger_mode"])
                self.assertFalse(config["tp_settings_applied"])

    def test_restore_final_persistence_failure_keeps_full_backup_recoverable(self):
        config = {
            "three_finger_mode": "swipes",
            "tp_settings_applied": True,
            "tp_settings_backup": {"A": 10, "B": None},
        }
        values = {"A": 1, "B": 2}
        saves = 0

        def save():
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError("final config save failed")

        manager = TouchpadSettingsManager(
            config,
            lambda name: values.get(name),
            lambda name, value: values.__setitem__(name, value),
            lambda name: values.pop(name, None),
            save,
        )
        with self.assertRaises(OSError):
            manager.restore()

        self.assertEqual({"A": 10, "B": None}, config["tp_settings_backup"])
        self.assertEqual("off", config["three_finger_mode"])
        self.assertFalse(config["tp_settings_applied"])

    def test_precision_touchpad_registry_has_one_dword_writer_adapter(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        dword_writes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "SetValueEx"
            and any(
                isinstance(arg, ast.Attribute) and arg.attr == "REG_DWORD"
                for arg in node.args
            )
        ]
        self.assertEqual(1, len(dword_writes))


class BatteryTests(unittest.TestCase):
    @staticmethod
    def _load_app_function(name, globals_):
        return InputLifecycleTests._load_app_definition(name, globals_)

    class FakeHidBackend:
        def __init__(self, devices, reports, features=None):
            self.devices = devices
            self.reports = reports
            self.features = features or {}
            self.enumerations = 0
            self.queries = []
            self.feature_queries = []

        def enumerate_devices(self):
            self.enumerations += 1
            return list(self.devices)

        def request_report(self, path):
            self.queries.append(path)
            value = self.reports[path]
            if isinstance(value, Exception):
                raise value
            return value

        def request_feature_report(self, path, report_id, length=2):
            self.feature_queries.append((path, report_id, length))
            value = self.features[path]
            if isinstance(value, Exception):
                raise value
            return value

    def test_poll_cycle_enumerates_once_and_queries_devices_independently(self):
        devices = [
            {"vendor_id": 0x004C, "product_id": 0x0267,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"keyboard"},
            {"vendor_id": 0x004C, "product_id": 0x0265,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"trackpad"},
        ]
        backend = self.FakeHidBackend(
            devices,
            {b"keyboard": b"\x90\x02\x4b", b"trackpad": OSError("asleep")},
        )
        published = []
        poller = BatteryPoller(backend, published.append, clock=lambda: 10.0)

        snapshot, delay = poller.poll_once()

        self.assertEqual(1, backend.enumerations)
        self.assertEqual([b"keyboard", b"trackpad"], backend.queries)
        self.assertEqual(BatteryResult("available", 75, True, False),
                         snapshot.keyboard)
        self.assertEqual("unavailable", snapshot.trackpad.status)
        self.assertIs(snapshot, published[0])
        self.assertGreaterEqual(poller.normal_interval, 180.0)
        self.assertGreater(poller.failure_backoff, poller.normal_interval)
        self.assertEqual(poller.normal_interval, delay)

    def test_mouse_v2_parses_input_0x90_and_rejects_phantom_zero(self):
        self.assertEqual(
            BatteryResult("available", 64, True, False),
            parse_apple_mouse_input_report(b"\x90\x02\x40"),
        )
        with self.assertRaises(ValueError):
            parse_apple_mouse_input_report(b"\x90\x00\x00")
        with self.assertRaises(ValueError):
            parse_apple_mouse_input_report(b"\x47\x40")
        self.assertEqual(
            BatteryResult("available", 53),
            parse_apple_mouse_feature_report(b"\x47\x35"),
        )
        with self.assertRaises(ValueError):
            parse_apple_mouse_feature_report(b"\x90\x00\x35")
        with self.assertRaises(ValueError):
            parse_apple_mouse_feature_report(b"\x47\x00")

    def test_mouse_v2_prefers_input_0x90_when_available(self):
        devices = [
            {"vendor_id": 0x004C, "product_id": 0x0269,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"mouse-battery"},
            {"vendor_id": 0x004C, "product_id": 0x0269,
             "usage_page": 0x0001, "usage": 0x0002, "path": b"mouse-pointer"},
        ]
        backend = self.FakeHidBackend(
            devices,
            {b"mouse-battery": b"\x90\x02\x46"},
            {b"mouse-pointer": b"\x47\x32"},
        )
        result = query_apple_mouse_battery(
            backend.enumerate_devices, backend.request_report,
            backend.request_feature_report,
        )
        self.assertEqual(BatteryResult("available", 70, True, False), result)
        self.assertEqual([b"mouse-battery"], backend.queries)
        self.assertEqual([], backend.feature_queries)

    def test_mouse_v2_uses_feature_0x47_when_input_0x90_is_unavailable(self):
        devices = [
            {"vendor_id": 0x004C, "product_id": 0x0269,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"mouse-battery"},
            {"vendor_id": 0x004C, "product_id": 0x0269,
             "usage_page": 0x0001, "usage": 0x0002, "path": b"mouse-pointer"},
        ]
        backend = self.FakeHidBackend(
            devices,
            {b"mouse-battery": OSError("Input 0x90 not exposed")},
            {b"mouse-pointer": b"\x47\x3a"},
        )
        result = query_apple_mouse_battery(
            backend.enumerate_devices, backend.request_report,
            backend.request_feature_report,
        )
        self.assertEqual(BatteryResult("available", 58), result)
        self.assertEqual(
            [(b"mouse-pointer", 0x47, 2)], backend.feature_queries
        )

    def test_mouse_v2_does_not_accept_a_wrong_feature_response(self):
        devices = [
            {"vendor_id": 0x004C, "product_id": 0x0269,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"mouse-battery"},
            {"vendor_id": 0x004C, "product_id": 0x0269,
             "usage_page": 0x0001, "usage": 0x0002, "path": b"mouse-pointer"},
        ]
        backend = self.FakeHidBackend(
            devices,
            {b"mouse-battery": OSError("Input 0x90 unavailable")},
            {b"mouse-pointer": b"\x90\x00\x55"},
        )
        result = query_apple_mouse_battery(
            backend.enumerate_devices, backend.request_report,
            backend.request_feature_report,
        )
        self.assertNotEqual("available", result.status)

    def test_mouse_v2_query_ignores_other_products(self):
        devices = [
            {"vendor_id": 0x004C, "product_id": 0x0323,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"mouse-v3"},
        ]
        backend = self.FakeHidBackend(devices, {b"mouse-v3": b"\x90\x00\x40"})
        result = query_apple_mouse_battery(
            backend.enumerate_devices, backend.request_report,
            backend.request_feature_report,
        )
        self.assertEqual("not_detected", result.status)
        self.assertEqual([], backend.queries)

    def test_hid_backend_closes_every_open_handle_even_when_query_fails(self):
        events = []

        class Handle:
            def open_path(self, path):
                events.append(("open", path))

            def get_input_report(self, report_id, length):
                events.append(("query", report_id, length))
                raise OSError("offline")

            def close(self):
                events.append(("close",))

        hid_module = mock.Mock()
        hid_module.device.side_effect = Handle
        backend = HidBatteryBackend(hid_module)

        with self.assertRaises(OSError):
            backend.request_report(b"new-path")

        self.assertEqual(
            [("open", b"new-path"), ("query", 0x90, 3), ("close",)], events
        )

    def test_hid_backend_closes_handle_after_successful_query(self):
        events = []

        class Handle:
            def open_path(self, path):
                events.append(("open", path))

            def get_input_report(self, report_id, length):
                events.append(("query", report_id, length))
                return b"\x90\x00\x2a"

            def close(self):
                events.append(("close",))

        hid_module = mock.Mock()
        hid_module.device.side_effect = Handle

        report = HidBatteryBackend(hid_module).request_report(b"fresh-path")

        self.assertEqual(b"\x90\x00\x2a", report)
        self.assertEqual(
            [("open", b"fresh-path"), ("query", 0x90, 3), ("close",)], events
        )

    def test_mouse_v2_is_included_in_the_complete_battery_snapshot(self):
        devices = [
            {"vendor_id": 0x004C, "product_id": 0x0269,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"mouse-battery"},
        ]
        backend = self.FakeHidBackend(devices, {b"mouse-battery": b"\x90\x00\x3f"})
        published = []
        snapshot, _ = BatteryPoller(
            backend, published.append, clock=lambda: 10.0
        ).poll_once()
        self.assertEqual(63, snapshot.mouse.percentage)
        self.assertEqual("not_detected", snapshot.keyboard.status)
        self.assertEqual("not_detected", snapshot.trackpad.status)
        self.assertIs(snapshot, published[0])

    def test_repeated_poll_cycles_reenumerate_and_use_fresh_device_paths(self):
        paths = iter((b"keyboard-old", b"keyboard-new"))

        class ChangingBackend:
            def __init__(self):
                self.enumerations = 0
                self.queries = []

            def enumerate_devices(self):
                self.enumerations += 1
                return [{
                    "vendor_id": 0x004C,
                    "product_id": 0x0267,
                    "usage_page": 0xFF00,
                    "usage": 0x14,
                    "path": next(paths),
                }]

            def request_report(self, path):
                self.queries.append(path)
                return b"\x90\x00\x2a"

        backend = ChangingBackend()
        poller = BatteryPoller(backend, lambda snapshot: None)

        poller.poll_once()
        poller.poll_once()

        self.assertEqual(2, backend.enumerations)
        self.assertEqual([b"keyboard-old", b"keyboard-new"], backend.queries)

    def test_snapshot_store_exposes_only_complete_immutable_snapshots(self):
        store = BatterySnapshotStore()
        backend = self.FakeHidBackend([], {})
        snapshot, _ = BatteryPoller(backend, store.publish, clock=lambda: 2.0).poll_once()

        self.assertIs(snapshot, store.snapshot())
        with self.assertRaises((AttributeError, TypeError)):
            snapshot.keyboard = BatteryResult("available", 1)

    def test_failed_reading_becomes_explicitly_stale_without_false_current_percent(self):
        now = [0.0]
        devices = [
            {"vendor_id": 0x004C, "product_id": 0x0267,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"keyboard"},
        ]
        backend = self.FakeHidBackend(devices, {b"keyboard": b"\x90\x04\x50"})
        poller = BatteryPoller(
            backend, lambda snapshot: None, clock=lambda: now[0], stale_after=600.0
        )
        first, _ = poller.poll_once()
        self.assertEqual(80, first.keyboard.percentage)

        backend.reports[b"keyboard"] = OSError("offline")
        now[0] = 300.0
        recent_failure, _ = poller.poll_once()
        self.assertEqual("unavailable", recent_failure.keyboard.status)
        self.assertFalse(recent_failure.keyboard.stale)
        self.assertIsNone(recent_failure.keyboard.percentage)

        now[0] = 601.0
        stale, _ = poller.poll_once()
        self.assertEqual("unavailable", stale.keyboard.status)
        self.assertTrue(stale.keyboard.stale)
        self.assertIsNone(stale.keyboard.percentage)
        self.assertEqual(80, stale.keyboard.last_percentage)
        self.assertEqual(
            "Magic Keyboard battery: unavailable; last known 80% (stale)",
            format_battery_label("Magic Keyboard", stale.keyboard),
        )

    def test_worker_shutdown_interrupts_long_backoff_wait_cleanly(self):
        published = threading.Event()
        stop = threading.Event()
        backend = self.FakeHidBackend([], {})
        poller = BatteryPoller(
            backend,
            lambda snapshot: published.set(),
            normal_interval=180.0,
            failure_backoff=900.0,
        )
        thread = threading.Thread(target=poller.run, args=(stop,))
        thread.start()
        self.assertTrue(published.wait(1.0))

        stop.set()
        thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, backend.enumerations)

    def test_all_nonvalid_states_have_distinct_dynamic_labels(self):
        labels = {
            status: format_battery_label("Magic Trackpad", BatteryResult(status))
            for status in ("loading", "not_detected", "unavailable", "unsupported")
        }

        self.assertEqual(4, len(set(labels.values())))
        self.assertIn("loading", labels["loading"])
        self.assertIn("not detected", labels["not_detected"])
        self.assertIn("unavailable", labels["unavailable"])
        self.assertIn("unsupported report", labels["unsupported"])

    def test_dynamic_tray_labels_read_one_immutable_snapshot(self):
        snapshot = BatterySnapshot(
            BatteryResult("available", 91), BatteryResult("not_detected"), 12.0
        )
        store = mock.Mock(snapshot=mock.Mock(return_value=snapshot))
        label = self._load_app_function(
            "battery_menu_label",
            {
                "battery_snapshots": store,
                "format_battery_label": format_battery_label,
            },
        )

        self.assertEqual("Magic Keyboard battery: 91%", label("keyboard"))
        self.assertEqual(
            "Magic Trackpad battery: not detected", label("trackpad")
        )
        self.assertEqual(2, store.snapshot.call_count)

    def test_each_complete_publication_requests_a_tray_refresh(self):
        store = mock.Mock()
        dispatcher = mock.Mock()
        publish = self._load_app_function(
            "publish_battery_snapshot",
            {
                "battery_snapshots": store,
                "battery_menu_refresh": dispatcher,
            },
        )
        states = (
            BatteryResult("available", 88),
            BatteryResult("available", 89, charging=True),
            BatteryResult("not_detected"),
            BatteryResult("unsupported"),
            BatteryResult("unavailable"),
            BatteryResult("unavailable", stale=True, last_percentage=89),
        )

        for index, state in enumerate(states):
            snapshot = BatterySnapshot(state, BatteryResult("loading"), float(index))
            publish(snapshot)
            self.assertIs(snapshot, store.publish.call_args.args[0])
            self.assertEqual(index + 1, dispatcher.schedule.call_count)

    def test_post_message_declaration_preserves_high_bit_hwnd_on_x64(self):
        self.assertEqual(8, ctypes.sizeof(ctypes.c_void_p))
        received = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        callback = callback_type(
            lambda hwnd, message, wparam, lparam: received.append(hwnd) or 1
        )

        class PostMessageFunction(ctypes._CFuncPtr):
            _flags_ = ctypes._FUNCFLAG_STDCALL
            _restype_ = wintypes.BOOL

        post_message = PostMessageFunction(
            ctypes.cast(callback, ctypes.c_void_p).value
        )
        high_bit_hwnd = (1 << 63) | 0x1234

        with self.assertRaises(ctypes.ArgumentError):
            post_message(high_bit_hwnd, 0x8000, 0, 0)
        self.assertEqual([], received)

        post_message.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        post_message.restype = wintypes.BOOL
        self.assertTrue(post_message(high_bit_hwnd, 0x8000, 0, 0))
        self.assertEqual([high_bit_hwnd], received)

        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()
        self.assertIn(
            "user32.PostMessageW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)",
            source,
        )
        self.assertIn("user32.PostMessageW.restype = w.BOOL", source)

    def test_windows_dispatcher_coalesces_worker_posts_and_rebuilds_on_ui_context(self):
        posted = []
        update_threads = []

        class Icon:
            _hwnd = 123

            def __init__(self):
                self._message_handlers = {}

            def update_menu(self):
                update_threads.append(threading.get_ident())

        icon = Icon()
        dispatcher_type = InputLifecycleTests._load_app_definition(
            "WindowsTrayMenuRefreshDispatcher", {"threading": threading}
        )
        dispatcher = dispatcher_type(
            icon,
            post_message=lambda hwnd, message, wp, lp: posted.append(
                (hwnd, message, wp, lp, threading.get_ident())
            ) or 1,
        )
        dispatcher.start()
        worker_id = []

        def publish_burst():
            worker_id.append(threading.get_ident())
            for _ in range(20):
                dispatcher.schedule()

        worker = threading.Thread(target=publish_burst)
        worker.start()
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(posted))
        self.assertEqual(worker_id[0], posted[0][4])
        self.assertEqual([], update_threads)

        ui_id = threading.get_ident()
        icon._message_handlers[dispatcher.message](0, 0)
        self.assertEqual([ui_id], update_threads)

        dispatcher.schedule()
        self.assertEqual(2, len(posted))

    def test_windows_dispatcher_shutdown_drops_pending_refresh_safely(self):
        updates = []

        class Icon:
            _hwnd = 456

            def __init__(self):
                self._message_handlers = {}

            def update_menu(self):
                updates.append("updated")

        icon = Icon()
        posts = []
        dispatcher_type = InputLifecycleTests._load_app_definition(
            "WindowsTrayMenuRefreshDispatcher", {"threading": threading}
        )
        dispatcher = dispatcher_type(
            icon,
            post_message=lambda *args: posts.append(args) or 1,
        )
        dispatcher.start()
        dispatcher.schedule()
        dispatcher.close()

        icon._message_handlers[dispatcher.message](0, 0)
        self.assertEqual([], updates)
        self.assertFalse(dispatcher.schedule())
        self.assertEqual(1, len(posts))

    def test_app_stop_battery_worker_signals_and_joins_live_worker(self):
        events = []

        class Stop:
            def set(self):
                events.append("set")

        class Thread:
            def join(self, timeout):
                events.append(("join", timeout))

            @staticmethod
            def is_alive():
                return False

        stop_worker = self._load_app_function(
            "stop_battery_worker",
            {
                "battery_stop": Stop(),
                "battery_thread": Thread(),
                "battery_lifecycle_lock": threading.Lock(),
                "battery_shutdown": False,
                "append_error": lambda message: events.append(message),
            },
        )

        self.assertTrue(stop_worker(timeout=0.25))
        self.assertEqual(["set", ("join", 0.25)], events)
        self.assertIsNone(stop_worker.__globals__["battery_thread"])

    def test_app_starts_isolated_battery_worker_and_stops_it_before_exit(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()

        self.assertIn("BatteryPoller(", source)
        self.assertIn("HidBatteryBackend(hidapi)", source)
        self.assertIn("target=battery_poller.run", source)
        self.assertIn("battery_stop.set()", source)
        self.assertIn("worker.join(", source)
        self.assertIn('battery_menu_label("keyboard")', source)
        self.assertIn('battery_menu_label("trackpad")', source)
        worker_section = source[source.index("def start_battery_worker"):source.index(
            "def stop_battery_worker"
        )]
        self.assertNotIn("update_menu", worker_section)

    def test_parse_valid_battery_report_and_status_bits(self):
        reading = parse_apple_battery_report(bytes((0x90, 0x06, 78)))

        self.assertEqual(78, reading.percentage)
        self.assertTrue(reading.charging)
        self.assertTrue(reading.fully_charged)

    def test_parser_rejects_wrong_shape_id_and_percentage(self):
        for report in (b"", b"\x90\x00", b"\x91\x00\x32",
                       b"\x90\x00\x65", b"\x90\x00\x32\x00"):
            with self.subTest(report=report):
                with self.assertRaises(ValueError):
                    parse_apple_battery_report(report)

    def test_query_filters_exact_vendor_collection_and_reports_states(self):
        devices = [
            {"vendor_id": 0x004C, "product_id": 0x0267,
             "usage_page": 0x01, "usage": 0x06, "path": b"keyboard"},
            {"vendor_id": 0x004C, "product_id": 0x0267,
             "usage_page": 0xFF00, "usage": 0x14, "path": b"battery"},
        ]

        result = query_apple_battery(
            0x0267,
            enumerate_devices=lambda: devices,
            request_report=lambda path: b"\x90\x00\x2a",
        )
        missing = query_apple_battery(
            0x0265,
            enumerate_devices=lambda: devices,
            request_report=lambda path: b"\x90\x00\x2a",
        )

        self.assertEqual(BatteryResult("available", 42, False, False), result)
        self.assertEqual("not_detected", missing.status)

    def test_query_failure_and_malformed_report_have_distinct_labels(self):
        devices = [{"vendor_id": 0x004C, "product_id": 0x0265,
                    "usage_page": 0xFF00, "usage": 0x14,
                    "path": b"battery"}]

        unavailable = query_apple_battery(
            0x0265,
            enumerate_devices=lambda: devices,
            request_report=lambda path: (_ for _ in ()).throw(OSError("asleep")),
        )
        unsupported = query_apple_battery(
            0x0265,
            enumerate_devices=lambda: devices,
            request_report=lambda path: b"\x90\x00\xff",
        )

        self.assertEqual("Magic Trackpad battery: unavailable",
                         format_battery_label("Magic Trackpad", unavailable))
        self.assertEqual("Magic Trackpad battery: unsupported report",
                         format_battery_label("Magic Trackpad", unsupported))


class Phase3InputBlockerTests(unittest.TestCase):
    def setup_gesture(self, mode="drag"):
        g, actions, suppression = GestureHardeningTests._gesture(mode)
        cfg = dict(three_finger_mode=mode, drag_gain=1.0, drag_grace_ms=350,
                   drag_start_units=30, swipe_units=300)
        g._frame.__globals__["config_value"] = cfg.__getitem__
        buttons, moves = [], []
        g._button = buttons.append
        g._move = lambda x, y: moves.append((x, y))
        return g, cfg, actions, suppression, buttons, moves

    def frame(self, g, when, contacts):
        g._contacts = lambda hdev, rep: contacts
        with mock.patch("time.monotonic", return_value=when):
            g._frame(7, b"fake parsed contact report")

    def start_drag(self, g):
        for t in (1.0, 1.03, 1.06, 1.08):
            self.frame(g, t, [(cid, 100 + cid, 100, True) for cid in (1, 2, 3)])
        self.frame(g, 1.09, [(cid, 140 + cid, 100, True) for cid in (1, 2, 3)])
        self.assertEqual("dragging", g._state)

    def mode_callback(self, g, cfg, suppression, save=lambda: None):
        set_mode = InputLifecycleTests._load_app_definition("set_tf_mode", dict(
            config=cfg, set_managed_mode=set_managed_mode,
            tp_settings=mock.Mock(enforce=lambda: []), save_config=lambda config: save(),
            release_custom_input=release_custom_input, three_finger_drag=g,
            suppress_pointer=suppression))
        return InputLifecycleTests._load_app_definition("on_mode_swipes", dict(
            set_tf_mode=set_mode, _update_menu=lambda icon: None))

    def test_restore_cleanup_failure_retains_recovery_until_retry(self):
        for failures in (("LEFTUP",), ("suppression",),
                         ("LEFTUP", "suppression", "save")):
            with self.subTest(failures=failures):
                g, cfg, _, suppression, buttons, _ = self.setup_gesture()
                self.start_drag(g)
                backup = {"ThreeFingerSlideEnabled": 1}
                cfg.update(tp_settings_backup=dict(backup), tp_settings_applied=True)
                active_failures = set(failures)
                writes, saved = [], []
                def button(down):
                    buttons.append(down)
                    if not down and "LEFTUP" in active_failures:
                        raise OSError("LEFTUP failed")
                def disable(active):
                    if "suppression" in active_failures:
                        raise OSError("suppression failed")
                def save():
                    if "save" in active_failures:
                        raise OSError("save failed")
                    saved.append(dict(cfg))
                g._button = button
                suppression.set.side_effect = disable
                manager = TouchpadSettingsManager(
                    cfg, lambda name: 0, lambda name, value: writes.append((name, value)),
                    lambda name: None, save)
                notify, menu = mock.Mock(), mock.Mock()
                callback = InputLifecycleTests._load_app_definition("on_restore_tp", dict(
                    config=cfg, save_config=lambda cfg: save(),
                    release_custom_input=release_custom_input, three_finger_drag=g,
                    suppress_pointer=suppression, tp_settings=manager,
                    _notify=notify, _update_menu=menu))
                callback(None, None)
                self.assertEqual([], writes, "native ownership restored despite failed cleanup")
                self.assertEqual(backup, cfg["tp_settings_backup"])
                self.assertEqual("off", cfg["three_finger_mode"])
                self.assertFalse(cfg["tp_settings_applied"])
                suppression.set.assert_called_with(False)
                self.assertEqual("Touchpad restore failed", notify.call_args.args[2])
                for failure in failures:
                    self.assertIn(failure + " failed", notify.call_args.args[1])
                menu.assert_called_once_with(None)
                if "save" not in failures:
                    self.assertEqual(backup, saved[-1]["tp_settings_backup"])
                    self.assertEqual("off", saved[-1]["three_finger_mode"])
                if "LEFTUP" in failures:
                    self.assertIn(g._state, ("dragging", "grace"))
                active_failures.clear()
                with mock.patch("time.monotonic", return_value=1.30):
                    g._maintenance()
                callback(None, None)
                self.assertEqual("idle", g._state)
                self.assertEqual(1, buttons.count(True))
                self.assertEqual([("ThreeFingerSlideEnabled", 1)], writes)
                self.assertEqual({}, cfg["tp_settings_backup"])
                self.assertEqual("Original touchpad settings restored", notify.call_args.args[2])

    def test_restore_serializes_cleanup_and_registry_replay_against_input(self):
        g, cfg, _, suppression, buttons, _ = self.setup_gesture()
        self.start_drag(g)
        cfg.update(tp_settings_backup={"ThreeFingerSlideEnabled": 1},
                   tp_settings_applied=True)
        observations = []

        def competing_input():
            # Nonblocking acquisition is an exact rendezvous, not a timed
            # assertion that a thread merely has not been scheduled yet.
            acquired = CONFIG_LOCK.acquire(blocking=False)
            observations.append(acquired)
            if acquired:
                CONFIG_LOCK.release()

        def check_seam():
            worker = threading.Thread(target=competing_input)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            # Reentrant delivery also must see Off before registry replay.
            for t in (1.20, 1.23, 1.26, 1.28, 1.29):
                self.frame(g, t, [(cid, (140 if t == 1.29 else 100) + cid,
                                  100, True) for cid in (1, 2, 3)])

        writes = []
        manager = TouchpadSettingsManager(
            cfg, lambda name: 0,
            lambda name, value: writes.append((name, value, g._state)),
            lambda name: None, lambda: None)
        original_abort = g.abort_gesture
        def abort():
            original_abort()
            check_seam()
        g.abort_gesture = abort
        def restore():
            check_seam()
            return manager.restore()
        notify = mock.Mock()
        callback = InputLifecycleTests._load_app_definition("on_restore_tp", dict(
            config=cfg, save_config=lambda cfg: None,
            release_custom_input=release_custom_input, three_finger_drag=g,
            suppress_pointer=suppression, tp_settings=mock.Mock(restore=restore),
            _notify=notify, _update_menu=mock.Mock()))
        callback(None, None)
        self.assertEqual([False, False], observations)
        self.assertEqual([True, False], buttons)
        self.assertEqual([("ThreeFingerSlideEnabled", 1, "idle")], writes)
        self.assertEqual("off", cfg["three_finger_mode"])
        self.assertEqual({}, cfg["tp_settings_backup"])
        self.assertIn("restored", notify.call_args.args[2])

    def test_exactly_three_reused_contacts_do_not_move_or_resume_drag(self):
        for initial_state in ("dragging", "grace"):
            with self.subTest(initial_state=initial_state):
                g, _, _, suppression, buttons, moves = self.setup_gesture()
                self.start_drag(g)
                if initial_state == "grace":
                    self.frame(g, 1.095, [(3, 143, 100, False)])
                    self.assertEqual("grace", g._state)
                self.frame(g, 1.10, [(1, 5000, 5000, True),
                                     (2, 143, 101, True), (3, 145, 102, True)])
                self.assertEqual(3, len(g._touch))
                self.assertFalse(g._quality(list(g._touch.values()), 1.10))
                suppression.set.assert_called_with(False)
                self.assertEqual([], moves)
                self.assertEqual("grace", g._state)
                deadline = g._grace_deadline
                # More rejected frames must not renew grace indefinitely.
                for t in (1.12, 1.14):
                    self.frame(g, t, [(1, 5000, 5000, True),
                                      (2, 143, 101, True), (3, 145, 102, True)])
                    self.assertEqual(deadline, g._grace_deadline)
                    self.assertEqual([], moves)
                # A mature fresh set resumes without a jump or second LEFTDOWN.
                self.frame(g, 1.17, [(1, 5000, 5000, True),
                                   (2, 143, 101, True), (3, 145, 102, True)])
                self.assertEqual("dragging", g._state)
                self.assertEqual([True], buttons)
                self.assertEqual([], moves)
                self.frame(g, 1.18, [(1, 5001, 5001, True),
                                    (2, 144, 102, True), (3, 146, 103, True)])
                self.assertEqual([(3.0, 3.0)], moves)

    def test_unqualified_exactly_three_contacts_expire_grace(self):
        g, _, _, suppression, buttons, moves = self.setup_gesture()
        self.start_drag(g)
        for t, x in ((1.10, 5000), (1.14, 100), (1.18, 5000),
                     (1.22, 100), (1.26, 5000), (1.30, 100),
                     (1.34, 5000), (1.38, 100), (1.42, 5000), (1.46, 100)):
            self.frame(g, t, [(1, x, 100, True), (2, 143, 100, True),
                              (3, 144, 100, True)])
        self.assertEqual(3, len(g._touch))
        self.assertEqual([], moves)
        self.assertEqual([True, False], buttons)
        self.assertEqual("idle", g._state)
        suppression.set.assert_called_with(False)

    def test_quit_fences_frames_during_cleanup_and_battery_join(self):
        for failures in (0, 1, 99):
            with self.subTest(leftup_failures=failures):
                g, cfg, actions, suppression, buttons, moves = self.setup_gesture()
                self.start_drag(g)
                def button(down):
                    buttons.append(down)
                    if not down and buttons.count(False) <= failures:
                        raise OSError("LEFTUP failed")
                g._button = button
                def late_input():
                    for t in (1.20, 1.23, 1.26, 1.28):
                        self.frame(g, t, [(cid, 100 + cid, 100, True)
                                          for cid in (1, 2, 3)])
                    self.frame(g, 1.29, [(cid, 140 + cid, 100, True)
                                         for cid in (1, 2, 3)])
                    with mock.patch("time.monotonic", return_value=1.30):
                        g._maintenance()
                stop_worker = mock.Mock(side_effect=late_input)
                suppression.shutdown.side_effect = late_input
                dispatcher = mock.Mock(close=mock.Mock(side_effect=late_input))
                exits, notify = [], mock.Mock()
                icon = mock.Mock()
                callback = InputLifecycleTests._load_app_definition("on_quit", dict(
                    actions=mock.Mock(), battery_menu_refresh=dispatcher,
                    shutdown_custom_input=shutdown_custom_input, three_finger_drag=g,
                    suppress_pointer=suppression, stop_battery_worker=stop_worker,
                    brightness=mock.Mock(last_backend="hardware"),
                    os=mock.Mock(_exit=exits.append), _notify=notify))
                callback(icon, None)
                self.assertEqual(1, buttons.count(True), "Quit reacquired LEFTDOWN")
                self.assertEqual(min(failures + 1, 3), buttons.count(False))
                self.assertEqual([], moves)
                self.assertEqual([], actions)
                self.assertEqual({}, g._touch)
                suppression.set.assert_called_with(False)
                self.assertEqual([0], exits)
                stop_worker.assert_called_once_with()
                suppression.shutdown.assert_called_once_with()
                dispatcher.close.assert_called_once_with()
                icon.stop.assert_called_once_with()
                self.assertEqual(failures > 2, notify.called)
                self.assertEqual("drag", cfg["three_finger_mode"])

    def test_drag_to_swipes_releases_before_callback_returns(self):
        g, cfg, _, suppression, buttons, _ = self.setup_gesture()
        self.start_drag(g)
        self.mode_callback(g, cfg, suppression)(None, None)
        self.assertEqual([True, False], buttons)
        self.assertEqual("idle", g._state)
        self.assertEqual({}, g._touch)
        suppression.set.assert_called_with(False)

    def test_mode_switch_waits_for_inflight_frame(self):
        g, cfg, _, suppression, buttons, _ = self.setup_gesture()
        self.start_drag(g)
        entered, resume, switching, saved = (threading.Event() for _ in range(4))
        errors = []
        def contacts(hdev, rep):
            entered.set()
            if not resume.wait(2):
                raise AssertionError("frame rendezvous timed out")
            return []
        g._contacts = contacts
        def run(fn):
            try:
                fn()
            except Exception as exc:
                errors.append(exc)
        callback = self.mode_callback(g, cfg, suppression, saved.set)
        def switch():
            switching.set()
            callback(None, None)
        frame = threading.Thread(target=lambda: run(lambda: g._frame(7, b"report")))
        mode = threading.Thread(target=lambda: run(switch))
        frame.start()
        try:
            self.assertTrue(entered.wait(2))
            mode.start()
            self.assertTrue(switching.wait(2))
            self.assertFalse(saved.wait(0.1), "mode changed inside an inflight frame")
        finally:
            resume.set()
            frame.join(2)
            if mode.ident is not None:
                mode.join(2)
        self.assertFalse(frame.is_alive())
        self.assertFalse(mode.is_alive())
        self.assertEqual([], errors)
        self.assertEqual([True, False], buttons)

    def test_failed_mode_release_retries_on_swipes_maintenance(self):
        g, cfg, actions, suppression, buttons, _ = self.setup_gesture()
        self.start_drag(g)
        def button(down):
            buttons.append(down)
            if buttons.count(False) <= 2:
                raise OSError("LEFTUP failed")
        g._button = button
        self.mode_callback(g, cfg, suppression)(None, None)
        self.assertEqual([True, False], buttons)
        self.assertIn(g._state, ("dragging", "grace"))
        suppression.set.assert_called_with(False)
        with mock.patch("time.monotonic", return_value=1.10):
            with self.assertRaisesRegex(OSError, "LEFTUP failed"):
                g._maintenance()
        self.assertEqual([True, False, False], buttons)
        self.assertEqual([], actions)
        with mock.patch("time.monotonic", return_value=1.11):
            g._maintenance()
        self.assertEqual([True, False, False, False], buttons)
        self.assertEqual("idle", g._state)
        self.assertEqual({}, g._touch)


class GestureHardeningTests(unittest.TestCase):
    @staticmethod
    def _gesture(mode="swipes"):
        actions = []
        suppression = mock.Mock()
        gesture_type = InputLifecycleTests._load_app_definition(
            "ThreeFingerDrag",
            {
                "ctypes": ctypes,
                "w": wintypes,
                "time": time,
                "threading": threading,
                "suppress_pointer": suppression,
                "config_value": lambda key: {
                    "three_finger_mode": mode,
                    "drag_gain": 1.0,
                    "drag_grace_ms": 350,
                    "drag_start_units": 30,
                    "swipe_units": 300,
                }[key],
                "contacts_are_stable": lambda entries, now, fresh, min_age: (
                    bool(entries)
                    and all(now - entry[2] <= fresh for entry in entries)
                    and all(now - entry[5] >= min_age for entry in entries)
                ),
                "expire_stale_contacts": expire_stale_contacts,
                "should_suppress_pointer": should_suppress_pointer,
                "actions": mock.Mock(put=actions.append),
                "SWIPE_ACTIONS": {
                    "left": "left", "right": "right", "up": "up", "down": "down"
                },
                "release_custom_input": release_custom_input,
                "require_single_input": require_single_input,
                "INPUT": object,
                "MOUSEINPUT": object,
                "MAGIC_EXTRA": 1,
                "user32": object(),
                "kernel32": object(),
                "is_target_trackpad_path": lambda path: True,
                "run_input_callback": run_input_callback,
                "append_error": lambda message: None,
            },
        )
        gesture = object.__new__(gesture_type)
        gesture._touch = {}
        gesture._pp = {}
        gesture._links = {}
        gesture._is_tp = {}
        gesture.TOUCH_TTL = 0.07
        gesture.FRESH = 0.06
        gesture.MIN_AGE = 0.06
        gesture.MAX_CONTACT_JUMP = 1500
        gesture.ALL_UP_DEBOUNCE = 0.05
        gesture._state = "idle"
        gesture._anchor = gesture._last = None
        gesture._grace_deadline = 0.0
        gesture._residual = [0.0, 0.0]
        gesture._all_up_since = None
        gesture._swipe_fired = False
        return gesture, actions, suppression

    @staticmethod
    def _entry(x, y, now, born=0.0):
        return [x, y, now, x, y, born]

    def test_reused_contact_id_large_jump_reanchors_without_drag_cursor_move(self):
        gesture, _, _ = self._gesture("drag")
        gesture._state = "dragging"
        gesture._touch[(7, 1)] = self._entry(100, 100, 1.0)
        gesture._contacts = lambda hdev, rep: [(1, 5000, 5000, True)]
        moves = []
        gesture._move = lambda dx, dy: moves.append((dx, dy))

        with mock.patch("time.monotonic", return_value=1.01):
            gesture._frame(7, b"report")

        self.assertEqual([], moves)
        self.assertEqual([5000, 5000, 1.01, 5000, 5000, 1.01],
                         gesture._touch[(7, 1)])
        self.assertEqual("grace", gesture._state)

    def test_padding_contact_id_cannot_turn_two_fingers_into_three(self):
        gesture, actions, suppression = self._gesture("swipes")
        gesture._touch = {
            (7, 7): self._entry(3700, 1700, 1.0, born=0.0),
            (7, 11): self._entry(2600, 1400, 1.0, born=0.0),
        }
        gesture._contacts = lambda hdev, rep: [
            (7, 3720, 1680, True),
            (11, 2620, 1380, True),
            (0xFFFF, 0, 0, True),
        ]

        with mock.patch("time.monotonic", return_value=1.01):
            gesture._frame(7, b"report")

        self.assertEqual({(7, 7), (7, 11)}, set(gesture._touch))
        suppression.set.assert_called_with(False)
        self.assertEqual("idle", gesture._state)
        self.assertEqual([], actions)

    def test_reused_id_does_not_turn_fired_epoch_into_duplicate_swipe(self):
        gesture, actions, _ = self._gesture("swipes")
        gesture._state = "fired"
        gesture._swipe_fired = True
        gesture._touch = {
            (7, cid): self._entry(100 + cid, 100, 1.0, born=0.0)
            for cid in (1, 2, 3)
        }
        gesture._contacts = lambda hdev, rep: [(1, 5000, 100, True)]

        with mock.patch("time.monotonic", return_value=1.01):
            gesture._frame(7, b"report")
        gesture._touch[(7, 1)][0] = 5500
        gesture._touch[(7, 2)][0] += 500
        gesture._touch[(7, 3)][0] += 500
        gesture._process_state(1.07, [])

        self.assertEqual([], actions)
        self.assertTrue(gesture._swipe_fired)

    def test_touchdown_epoch_survives_two_four_churn_until_debounced_all_up(self):
        gesture, actions, _ = self._gesture("swipes")
        gesture._state = "fired"
        gesture._swipe_fired = True
        for count in (2, 4, 3):
            gesture._touch = {
                (7, cid): self._entry(100 + cid, 100, 2.0, born=0.0)
                for cid in range(count)
            }
            gesture._frame_swipes(count, 2.0)
        self.assertEqual([], actions)
        self.assertEqual("fired", gesture._state)

        gesture._touch.clear()
        gesture._frame_swipes(0, 2.01)
        self.assertTrue(gesture._swipe_fired)
        gesture._touch = {
            (7, cid): self._entry(100 + cid, 100, 2.02, born=0.0)
            for cid in range(3)
        }
        gesture._frame_swipes(3, 2.02)
        self.assertEqual("fired", gesture._state)

        gesture._touch.clear()
        gesture._frame_swipes(0, 2.03)
        gesture._frame_swipes(0, 2.09)
        self.assertFalse(gesture._swipe_fired)
        self.assertEqual("idle", gesture._state)

    def test_silence_expiry_fails_open_but_does_not_skip_all_up_debounce(self):
        gesture, _, suppression = self._gesture("swipes")
        gesture._state = "fired"
        gesture._swipe_fired = True
        gesture._touch = {
            (7, cid): self._entry(100, 100, 3.0, born=2.0) for cid in range(3)
        }

        gesture._process_state(3.08, [])

        self.assertEqual({}, gesture._touch)
        suppression.set.assert_called_with(False)
        self.assertTrue(gesture._swipe_fired)

    def test_disconnect_clears_only_that_device_and_releases_input(self):
        gesture, _, suppression = self._gesture("drag")
        gesture._state = "dragging"
        gesture._touch = {
            (7, 1): self._entry(100, 100, 1.0),
            (8, 1): self._entry(200, 200, 1.0),
        }
        released = []
        gesture._button = lambda down: released.append(down)

        gesture.device_disconnected(7)

        self.assertEqual({}, gesture._touch)
        self.assertEqual([False], released)
        suppression.set.assert_called_with(False)
        self.assertEqual("idle", gesture._state)

    def test_parser_modeoff_restore_and_shutdown_paths_all_fail_open(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()
        self.assertIn("self.abort_gesture", source[source.index("if msg == self.WM_INPUT"):])
        self.assertIn("three_finger_drag.abort_gesture", source[source.index("def on_mode_off"):])
        self.assertIn("three_finger_drag.abort_gesture", source[source.index("def on_restore_tp"):])
        self.assertIn("shutdown_custom_input(\n        three_finger_drag.abort_gesture", source)
        self.assertIn("RIDEV_DEVNOTIFY", source)
        self.assertIn("WM_INPUT_DEVICE_CHANGE", source)

    def test_parser_emits_lift_for_every_tip_confidence_matrix_entry(self):
        gesture, _, _ = self._gesture("swipes")
        gesture._links = {7: [3]}
        gesture._preparsed = lambda hdev: object()

        class Hid:
            usages = set()

            @staticmethod
            def HidP_GetUsageValue(kind, page, link, usage, value, pp, rep, length):
                ctypes.cast(value, ctypes.POINTER(ctypes.c_ulong))[0] = {
                    gesture.U_X: 100,
                    gesture.U_Y: 200,
                    gesture.U_CID: 9,
                }[usage]
                return gesture.HIDP_SUCCESS

            def HidP_GetUsages(self, kind, page, link, values, count, pp, rep, length):
                for index, usage in enumerate(sorted(self.usages)):
                    values[index] = usage
                ctypes.cast(count, ctypes.POINTER(ctypes.c_ulong))[0] = len(self.usages)
                return gesture.HIDP_SUCCESS

        gesture.hid = Hid()
        for tip, confidence in ((False, False), (False, True),
                                (True, False), (True, True)):
            with self.subTest(tip=tip, confidence=confidence):
                gesture.hid.usages = {
                    usage for enabled, usage in (
                        (tip, gesture.U_TIP), (confidence, gesture.U_CONF)
                    ) if enabled
                }
                contacts = gesture._contacts(7, b"report")
                self.assertEqual([(9, 100, 200, tip and confidence)], contacts)

    def test_injected_or_app_tagged_mouse_events_bypass_suppression(self):
        move = 0x0200
        self.assertFalse(should_suppress_mouse_event(move, 0x01, 0, True))
        self.assertFalse(should_suppress_mouse_event(move, 0, 0x50454152, True))
        self.assertTrue(should_suppress_mouse_event(move, 0, 0, True))
        self.assertFalse(should_suppress_mouse_event(move, 0, 0, False))


class ContactSuppressionTests(unittest.TestCase):
    def test_only_magic_trackpad_ptp_path_is_accepted(self):
        magic = (
            r"\\?\HID#{00001124-0000-1000-8000-00805f9b34fb}"
            r"_VID&0001004c_PID&0265&Col01&Col01#a&2727b852&0&0000"
        )
        other = r"\\?\HID#VID&1234_PID&5678&Col01#1&2&3"

        self.assertTrue(is_target_trackpad_path(magic))
        self.assertFalse(is_target_trackpad_path(other))

    def test_contact_id_churn_does_not_suppress_one_finger_pointer_motion(self):
        now = 10.0
        contacts = {
            4: [100, 100, 9.91, 90, 90, 9.70],
            7: [102, 101, 9.94, 95, 95, 9.80],
            9: [103, 102, 10.00, 103, 102, 9.99],
        }

        active = should_suppress_pointer(
            contacts, now=now, fresh=0.06, min_age=0.06
        )

        self.assertFalse(active)

    def test_three_stable_contacts_enable_pointer_suppression(self):
        now = 10.0
        contacts = {
            1: [100, 100, 9.98, 90, 90, 9.00],
            2: [200, 100, 9.97, 190, 90, 9.00],
            3: [300, 100, 9.96, 290, 90, 9.00],
        }

        active = should_suppress_pointer(
            contacts, now=now, fresh=0.06, min_age=0.06
        )

        self.assertTrue(active)

    def test_timer_expiry_releases_contacts_when_no_lift_report_arrives(self):
        contacts = {
            1: [100, 100, 4.90, 90, 90, 4.00],
            2: [200, 100, 4.98, 190, 90, 4.00],
            3: [300, 100, 4.99, 290, 90, 4.00],
        }

        expired = expire_stale_contacts(contacts, now=5.0, ttl=0.07)

        self.assertEqual([1], expired)
        self.assertEqual({2, 3}, set(contacts))


class AutostartProbeTests(unittest.TestCase):
    class FakeWinreg:
        """Minimal read-only winreg stand-in; never touches the real registry."""

        HKEY_CURRENT_USER = "HKCU"
        KEY_SET_VALUE = 2
        KEY_QUERY_VALUE = 1
        REG_SZ = 1

        def __init__(self, values=None, open_error=None, query_error=None):
            self.values = {} if values is None else dict(values)
            self.open_error = open_error
            self.query_error = query_error
            self.deleted = []
            self.written = []

        def OpenKey(self, root, path, reserved=0, access=0):
            if self.open_error is not None:
                raise self.open_error
            # winreg's default access includes KEY_QUERY_VALUE. When a caller
            # supplies an explicit mask, model it faithfully so a write-only
            # handle cannot accidentally make readback tests pass.
            return self._Key(self, access or self.KEY_QUERY_VALUE)

        def QueryValueEx(self, key, name):
            if not key.access & self.KEY_QUERY_VALUE:
                raise PermissionError(5, "access denied")
            if self.query_error is not None:
                raise self.query_error
            if name not in self.values:
                raise FileNotFoundError(2, "value not found")
            return (self.values[name], self.REG_SZ)

        def DeleteValue(self, key, name):
            self.deleted.append(name)
            if name not in self.values:
                raise FileNotFoundError(2, "value not found")
            del self.values[name]

        def SetValueEx(self, key, name, reserved, kind, value):
            self.written.append((name, value))
            self.values[name] = value

        class _Key:
            def __init__(self, owner, access):
                self.owner = owner
                self.access = access

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

    def _autostart_state(self, winreg_stub, expected_command='"D:\\App\\P.exe"'):
        from pearipherals_support import classify_autostart

        return InputLifecycleTests._load_app_definition("autostart_state", {
            "winreg": winreg_stub,
            "RUN_KEY": "Software\\Fake\\Run",
            "RUN_NAME": "Pearipherals",
            "classify_autostart": classify_autostart,
            "autostart_command": lambda: expected_command,
        })

    def test_missing_run_value_is_the_only_confirmed_off_state(self):
        winreg_stub = self.FakeWinreg(values={})

        self.assertEqual("off", self._autostart_state(winreg_stub)())

    def test_matching_command_is_current_and_moved_command_is_stale(self):
        current = self.FakeWinreg(values={"Pearipherals": '"D:\\App\\P.exe"'})
        moved = self.FakeWinreg(values={"Pearipherals": '"C:\\Old\\P.exe"'})

        self.assertEqual("current", self._autostart_state(current)())
        self.assertEqual("stale", self._autostart_state(moved)())

    def test_non_absence_read_failures_are_unavailable_never_off(self):
        failures = (
            PermissionError(5, "access denied"),
            OSError(1018, "key marked for deletion"),
        )

        for error in failures:
            with self.subTest(error=error):
                on_open = self.FakeWinreg(open_error=error)
                on_query = self.FakeWinreg(query_error=error)

                self.assertEqual("unavailable", self._autostart_state(on_open)())
                self.assertEqual("unavailable", self._autostart_state(on_query)())

    def test_missing_run_key_itself_is_confirmed_off(self):
        winreg_stub = self.FakeWinreg(
            open_error=FileNotFoundError(2, "key not found")
        )

        self.assertEqual("off", self._autostart_state(winreg_stub)())

    def test_boolean_autostart_api_is_preserved_for_existing_callers(self):
        from pearipherals_support import classify_autostart

        def build(winreg_stub):
            return InputLifecycleTests._load_app_definition("autostart_get", {
                "autostart_state": self._autostart_state(winreg_stub),
                "classify_autostart": classify_autostart,
            })

        on = self.FakeWinreg(values={"Pearipherals": '"D:\\App\\P.exe"'})
        stale = self.FakeWinreg(values={"Pearipherals": '"C:\\Old\\P.exe"'})
        off = self.FakeWinreg(values={})
        broken = self.FakeWinreg(open_error=PermissionError(5, "denied"))

        self.assertIs(True, build(on)())
        self.assertIs(True, build(stale)())
        self.assertIs(False, build(off)())
        self.assertIs(False, build(broken)())


class AutostartRemovalTests(unittest.TestCase):
    def _autostart_set(self, winreg_stub):
        return InputLifecycleTests._load_app_definition("autostart_set", {
            "winreg": winreg_stub,
            "RUN_KEY": "Software\\Fake\\Run",
            "RUN_NAME": "Pearipherals",
            "OLD_RUN_NAMES": ("MagicKeys", "MagicSuite"),
            "autostart_command": lambda: '"D:\\App\\P.exe"',
        })

    def test_disabling_removes_current_and_every_legacy_run_value(self):
        winreg_stub = AutostartProbeTests.FakeWinreg(values={
            "Pearipherals": '"D:\\App\\P.exe"',
            "MagicKeys": '"C:\\Old\\MagicKeys.exe"',
            "MagicSuite": '"C:\\Old\\MagicSuite.exe"',
            "Unrelated": "keep me",
        })

        self._autostart_set(winreg_stub)(False)

        self.assertEqual(
            ["Pearipherals", "MagicKeys", "MagicSuite"], winreg_stub.deleted
        )
        self.assertEqual({"Unrelated": "keep me"}, winreg_stub.values)

    def test_already_absent_values_are_not_a_failure(self):
        winreg_stub = AutostartProbeTests.FakeWinreg(values={})

        self._autostart_set(winreg_stub)(False)

        self.assertEqual(
            ["Pearipherals", "MagicKeys", "MagicSuite"], winreg_stub.deleted
        )

    def test_non_absence_delete_failures_propagate_instead_of_being_swallowed(self):
        class DenyingWinreg(AutostartProbeTests.FakeWinreg):
            def DeleteValue(self, key, name):
                self.deleted.append(name)
                raise PermissionError(5, "access denied")

        winreg_stub = DenyingWinreg(values={"Pearipherals": '"D:\\App\\P.exe"'})

        with self.assertRaises(PermissionError):
            self._autostart_set(winreg_stub)(False)

    def test_every_name_is_attempted_before_a_failure_is_raised(self):
        class PartialWinreg(AutostartProbeTests.FakeWinreg):
            def DeleteValue(self, key, name):
                self.deleted.append(name)
                if name == "Pearipherals":
                    raise PermissionError(5, "access denied")
                self.values.pop(name, None)

        winreg_stub = PartialWinreg(values={
            "Pearipherals": '"D:\\App\\P.exe"',
            "MagicKeys": '"C:\\Old\\MagicKeys.exe"',
            "MagicSuite": '"C:\\Old\\MagicSuite.exe"',
        })

        with self.assertRaises(PermissionError):
            self._autostart_set(winreg_stub)(False)

        self.assertEqual(
            ["Pearipherals", "MagicKeys", "MagicSuite"], winreg_stub.deleted
        )
        self.assertEqual({"Pearipherals": '"D:\\App\\P.exe"'}, winreg_stub.values)

    def test_enabling_still_writes_only_the_current_run_value(self):
        winreg_stub = AutostartProbeTests.FakeWinreg(values={})

        self._autostart_set(winreg_stub)(True)

        self.assertEqual([("Pearipherals", '"D:\\App\\P.exe"')], winreg_stub.written)
        self.assertEqual([], winreg_stub.deleted)


class RawInputRegistrationStateTests(unittest.TestCase):
    @staticmethod
    def _gesture_type(user32_stub):
        return InputLifecycleTests._load_app_definition(
            "ThreeFingerDrag",
            {
                "ctypes": ctypes,
                "w": wintypes,
                "time": time,
                "threading": threading,
                "user32": user32_stub,
                "kernel32": mock.Mock(GetModuleHandleW=lambda name: 1),
                "suppress_pointer": mock.Mock(),
                "config_value": lambda key: 0,
                "actions": mock.Mock(),
                "SWIPE_ACTIONS": {},
                "release_custom_input": release_custom_input,
                "require_single_input": require_single_input,
                "run_input_callback": run_input_callback,
                "append_error": lambda message: None,
                "is_target_trackpad_path": lambda path: True,
                "contacts_are_stable": lambda *a, **k: False,
                "expire_stale_contacts": expire_stale_contacts,
                "should_suppress_pointer": should_suppress_pointer,
                "INPUT": object,
                "MOUSEINPUT": object,
                "MAGIC_EXTRA": 1,
            },
        )

    @staticmethod
    def _blank_gesture(user32_stub=None):
        gesture_type = RawInputRegistrationStateTests._gesture_type(
            user32_stub if user32_stub is not None else mock.Mock()
        )
        gesture = object.__new__(gesture_type)
        gesture._raw_input_state = "not started"
        gesture._raw_input_lock = threading.Lock()
        gesture._thread = None
        gesture._wndproc_ref = None
        return gesture

    def test_state_is_not_started_before_the_listener_thread_runs(self):
        self.assertEqual("not started", self._blank_gesture().raw_input_state())

    def test_start_reports_starting_until_registration_completes(self):
        gesture = self._blank_gesture()
        started = []
        with mock.patch.object(
            threading, "Thread",
            lambda *a, **k: mock.Mock(start=lambda: started.append(True)),
        ):
            gesture.start()

        self.assertEqual([True], started)
        self.assertEqual("starting", gesture.raw_input_state())

    def test_successful_registration_reports_listener_registered(self):
        gesture = self._blank_gesture()
        gesture._note_raw_input_state("starting")

        gesture._note_raw_input_state("listener registered")

        self.assertEqual("listener registered", gesture.raw_input_state())

    def test_registration_failure_reports_unavailable(self):
        gesture = self._blank_gesture()
        gesture._note_raw_input_state("starting")

        gesture._note_raw_input_state("unavailable")

        self.assertEqual("unavailable", gesture.raw_input_state())

    def test_state_is_rejected_when_it_is_not_a_conservative_enum(self):
        from pearipherals_support import SUPPORT_ENUMS

        gesture = self._blank_gesture()

        for value in ("listening", "registered", "device present", "ok"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    gesture._note_raw_input_state(value)

        for value in SUPPORT_ENUMS["raw_input_state"]:
            with self.subTest(value=value):
                gesture._note_raw_input_state(value)
                self.assertEqual(value, gesture.raw_input_state())

    def test_registration_state_exposes_no_handle_or_device_path(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        gesture_node = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ThreeFingerDrag"
        )
        probe = next(
            node for node in gesture_node.body
            if isinstance(node, ast.FunctionDef) and node.name == "raw_input_state"
        )
        body = list(probe.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            body = body[1:]          # exclude the docstring, inspect real code
        returned = " ".join(ast.dump(node) for node in body)

        for leak in ("hwnd", "hDevice", "hdev", "_pp", "_is_tp", "DEVICENAME", "path"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, returned)

    def test_run_records_unavailable_when_registration_or_timer_fails(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()

        self.assertIn('self._note_raw_input_state("unavailable")', source)
        self.assertIn('self._note_raw_input_state("listener registered")', source)
        self.assertIn('self._note_raw_input_state("starting")', source)


class DriverServiceProbeTests(unittest.TestCase):
    class RecordingWinreg(AutostartProbeTests.FakeWinreg):
        """Read-only stand-in that records every access and refuses writes."""

        HKEY_LOCAL_MACHINE = "HKLM"
        KEY_READ = 0x20019

        def __init__(self, present=True, open_error=None):
            super().__init__(values={})
            self.present = present
            self.open_error = open_error
            self.opened = []

        def OpenKey(self, root, path, reserved=0, access=0):
            self.opened.append((root, path, reserved, access))
            if self.open_error is not None:
                raise self.open_error
            if not self.present:
                raise FileNotFoundError(2, "key not found")
            return self._Key(self, access or self.KEY_READ)

        def SetValueEx(self, *a, **k):
            raise AssertionError("driver probe must never write to the registry")

        def DeleteValue(self, *a, **k):
            raise AssertionError("driver probe must never delete registry values")

        def CreateKey(self, *a, **k):
            raise AssertionError("driver probe must never create registry keys")

    def _probe(self, winreg_stub):
        return InputLifecycleTests._load_app_definition("driver_service_state", {
            "winreg": winreg_stub,
            "DRIVER_SERVICE_KEY": r"SYSTEM\CurrentControlSet\Services\AmtPtpHidFilter",
        })

    def test_present_service_key_reports_service_registration_detected(self):
        winreg_stub = self.RecordingWinreg(present=True)

        self.assertEqual(
            "service registration detected", self._probe(winreg_stub)()
        )

    def test_missing_service_key_reports_not_detected(self):
        winreg_stub = self.RecordingWinreg(present=False)

        self.assertEqual("not detected", self._probe(winreg_stub)())

    def test_probe_reads_the_expected_service_key_read_only(self):
        winreg_stub = self.RecordingWinreg(present=True)

        self._probe(winreg_stub)()

        self.assertEqual(1, len(winreg_stub.opened))
        root, path, _reserved, access = winreg_stub.opened[0]
        self.assertEqual("HKLM", root)
        self.assertEqual(
            r"SYSTEM\CurrentControlSet\Services\AmtPtpHidFilter", path
        )
        self.assertEqual(winreg_stub.KEY_READ, access)
        self.assertEqual([], winreg_stub.written)
        self.assertEqual([], winreg_stub.deleted)

    def test_probe_failure_is_unavailable_and_never_crashes_or_leaks(self):
        failures = (
            PermissionError(5, r"access denied reading D:\Portable\driver"),
            OSError(1018, "key marked for deletion"),
            RuntimeError(r"unexpected \\?\C:\Users\name failure"),
        )

        for error in failures:
            with self.subTest(error=type(error).__name__):
                winreg_stub = self.RecordingWinreg(open_error=error)

                result = self._probe(winreg_stub)()

                self.assertEqual("unavailable", result)
                for leak in ("Portable", "Users", "denied", "1018", "\\\\?\\"):
                    self.assertNotIn(leak, result)

    def test_probe_result_is_always_a_conservative_fixed_enum(self):
        from pearipherals_support import SUPPORT_ENUMS

        results = {
            self._probe(self.RecordingWinreg(present=True))(),
            self._probe(self.RecordingWinreg(present=False))(),
            self._probe(self.RecordingWinreg(open_error=OSError(5, "x")))(),
        }

        self.assertEqual(SUPPORT_ENUMS["driver_state"], frozenset(results))
        self.assertNotIn("installed", " ".join(results))


class SupportSnapshotCompositionTests(unittest.TestCase):
    class CountingBatteryStore:
        """Cached publisher: snapshot() reads state, it never enumerates HID."""

        def __init__(self, snapshot):
            self._snapshot = snapshot
            self.reads = 0

        def snapshot(self):
            self.reads += 1
            return self._snapshot

    @staticmethod
    def _globals(**overrides):
        from pearipherals_support import (
            SupportSnapshot,
            battery_state,
            classify_removal_readiness,
        )
        from pearipherals_version import APP_VERSION, BuildIdentity

        available = BatteryResult("available", 80)
        store = SupportSnapshotCompositionTests.CountingBatteryStore(
            BatterySnapshot(available, BatteryResult("not_detected"), 1.0)
        )
        app_config = {
            "three_finger_mode": "swipes",
            "tp_settings_applied": True,
            "tp_settings_backup": {},
        }
        base = {
            "SupportSnapshot": SupportSnapshot,
            "BuildIdentity": BuildIdentity,
            "APP_VERSION": APP_VERSION,
            "battery_state": battery_state,
            "classify_removal_readiness": classify_removal_readiness,
            "build_identity": lambda: BuildIdentity(APP_VERSION, "git:" + "a" * 40),
            "UNKNOWN_BUILD_ID": "git:" + "0" * 40,
            "IS_FROZEN": True,
            "battery_snapshots": store,
            "config": app_config,
            "config_value": lambda key, default=None: app_config.get(key, default),
            "CONFIG_RECOVERY_ERROR": None,
            "LIFECYCLE_STATE": "version changed",
            "windows_release_info": lambda: ("Windows 11", "26100", "AMD64"),
            "driver_service_state": lambda: "service registration detected",
            "three_finger_drag": mock.Mock(
                raw_input_state=lambda: "listener registered"
            ),
            "tp_settings": mock.Mock(
                status_text=lambda: "applied",
                natural_scroll_get=lambda: False,
            ),
            "autostart_state": lambda: "current",
            "legacy_autostart_state": lambda: "absent",
        }
        base.update(overrides)
        return base, store, app_config

    def _build(self, **overrides):
        globals_, store, app_config = self._globals(**overrides)
        globals_["_safe_state"] = InputLifecycleTests._load_app_definition(
            "_safe_state", {}
        )
        builder = InputLifecycleTests._load_app_definition(
            "build_support_snapshot", globals_
        )
        return builder, store, app_config

    def test_snapshot_composes_fixed_states_from_every_probe(self):
        build, _store, _config = self._build()

        snapshot = build()

        self.assertEqual("frozen", snapshot.mode)
        self.assertEqual("git:" + "a" * 40, snapshot.identity.build_id)
        self.assertEqual("Windows 11", snapshot.windows_version)
        self.assertEqual("loaded", snapshot.config_state)
        self.assertEqual("available", snapshot.keyboard_battery)
        self.assertEqual("not detected", snapshot.trackpad_battery)
        self.assertEqual("service registration detected", snapshot.driver_state)
        self.assertEqual("listener registered", snapshot.raw_input_state)
        self.assertEqual("swipes", snapshot.gesture_mode)
        self.assertEqual("applied", snapshot.touchpad_settings)
        self.assertEqual("classic", snapshot.scroll_direction)
        self.assertEqual("current", snapshot.autostart)
        self.assertEqual("version changed", snapshot.lifecycle)
        self.assertEqual("action required", snapshot.removal_readiness)

    def test_gesture_readiness_requires_touchpad_ownership_settings_to_be_applied(self):
        build, _store, _config = self._build(
            tp_settings=mock.Mock(
                status_text=lambda: "needs apply", natural_scroll_get=lambda: False
            )
        )

        snapshot = build()

        self.assertEqual("listener registered", snapshot.raw_input_state)
        self.assertEqual("needs apply", snapshot.touchpad_settings)
        self.assertEqual("action required", snapshot.gesture_readiness)

    def test_snapshot_reads_one_cached_battery_snapshot_without_enumerating(self):
        build, store, _config = self._build()

        build()

        self.assertEqual(1, store.reads)

    def test_recovery_required_config_is_reported_without_exception_text(self):
        build, _store, _config = self._build(
            CONFIG_RECOVERY_ERROR=OSError(r"bad config at D:\Portable\p.json")
        )

        snapshot = build()

        self.assertEqual("recovery required", snapshot.config_state)
        self.assertNotIn("Portable", str(snapshot))

    def test_natural_scroll_preference_maps_to_the_scroll_direction_enum(self):
        build, _store, _config = self._build(
            tp_settings=mock.Mock(
                status_text=lambda: "applied", natural_scroll_get=lambda: True
            )
        )

        self.assertEqual("natural", build().scroll_direction)

    def test_probe_failures_degrade_to_unavailable_instead_of_crashing(self):
        def boom():
            raise OSError(r"denied at \\server\share")

        build, _store, _config = self._build(
            tp_settings=mock.Mock(status_text=boom, natural_scroll_get=boom),
            autostart_state=boom,
            windows_release_info=boom,
        )

        snapshot = build()

        self.assertEqual("unavailable", snapshot.touchpad_settings)
        self.assertEqual("unavailable", snapshot.scroll_direction)
        self.assertEqual("unavailable", snapshot.autostart)
        self.assertEqual("unavailable", snapshot.windows_version)
        self.assertNotIn("server", str(snapshot))

    def test_removal_readiness_is_ready_only_when_all_state_is_clear(self):
        build, _store, app_config = self._build(
            autostart_state=lambda: "off",
            tp_settings=mock.Mock(
                status_text=lambda: "original settings",
                natural_scroll_get=lambda: False,
            ),
        )
        app_config["three_finger_mode"] = "off"
        app_config["tp_settings_applied"] = False
        app_config["tp_settings_backup"] = {}
        app_config["mac_fkeys"] = False

        self.assertEqual("ready", build().removal_readiness)

    def test_snapshot_building_never_enumerates_hid_devices(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        builder = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "build_support_snapshot"
        )
        dumped = ast.dump(builder)

        for forbidden in (
            "enumerate", "hidapi", "HidBatteryBackend", "start_battery_worker",
            "poll_once", "request_report",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, dumped)


class WindowsReleaseInfoTests(unittest.TestCase):
    def test_windows_11_uses_build_when_kernel_release_still_says_10(self):
        fake_platform = mock.Mock(
            system=lambda: "Windows",
            release=lambda: "10",
            version=lambda: "10.0.26200",
            machine=lambda: "AMD64",
        )
        release_info = InputLifecycleTests._load_app_definition(
            "windows_release_info", {"platform": fake_platform}
        )

        self.assertEqual(
            ("Windows 11", "10.0.26200", "AMD64"), release_info()
        )


class AboutDialogTests(unittest.TestCase):
    @staticmethod
    def _source():
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            return stream.read()

    def test_message_box_declares_pointer_sized_prototypes(self):
        source = self._source()

        for declaration in (
            "user32.MessageBoxW.argtypes = (w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT)",
            "user32.MessageBoxW.restype = ctypes.c_int",
        ):
            with self.subTest(declaration=declaration):
                self.assertTrue(
                    declaration in source,
                    f"missing pointer-sized declaration: {declaration}",
                )

    def test_about_dialog_is_ownerless_so_hidden_tray_hwnd_cannot_steal_activation(self):
        calls = []
        show_about = InputLifecycleTests._load_app_definition("show_about_dialog", {
            "user32": mock.Mock(MessageBoxW=lambda *a: calls.append(a) or 1),
            "build_support_snapshot": lambda: "SNAP",
            "format_about": lambda snapshot: f"about:{snapshot}",
            "append_error": lambda message: None,
            "DIALOG_LOCK": threading.Lock(),
            "threading": threading,
            "MB_OK": 0x0,
            "MB_ICONINFORMATION": 0x40,
            "MB_SETFOREGROUND": 0x10000,
        })

        show_about()

        self.assertEqual(1, len(calls))
        hwnd, text, title, flags = calls[0]
        self.assertIsNone(hwnd)
        self.assertEqual("about:SNAP", text)
        self.assertEqual("Pearipherals — About / status", title)
        self.assertEqual(0x0 | 0x40 | 0x10000, flags)

    def test_about_message_box_runs_off_the_pystray_callback_thread(self):
        caller_thread = threading.get_ident()
        dialog_threads = []
        show_about = InputLifecycleTests._load_app_definition("show_about_dialog", {
            "user32": mock.Mock(
                MessageBoxW=lambda *args: dialog_threads.append(
                    threading.get_ident()
                ) or 1
            ),
            "build_support_snapshot": lambda: "SNAP",
            "format_about": lambda snapshot: f"about:{snapshot}",
            "append_error": lambda message: None,
            "DIALOG_LOCK": threading.Lock(),
            "threading": threading,
            "MB_OK": 0x0,
            "MB_ICONINFORMATION": 0x40,
            "MB_SETFOREGROUND": 0x10000,
        })

        show_about()

        self.assertEqual(1, len(dialog_threads))
        self.assertNotEqual(caller_thread, dialog_threads[0])

    def test_about_dialog_blocks_reentrant_stack_but_allows_later_open(self):
        calls = []
        show_about = None

        def message_box(*args):
            calls.append(args)
            if len(calls) == 1:
                show_about()
            return 1

        show_about = InputLifecycleTests._load_app_definition("show_about_dialog", {
            "user32": mock.Mock(MessageBoxW=message_box),
            "build_support_snapshot": lambda: "SNAP",
            "format_about": lambda snapshot: f"about:{snapshot}",
            "append_error": lambda message: None,
            "DIALOG_LOCK": threading.Lock(),
            "threading": threading,
            "MB_OK": 0x0,
            "MB_ICONINFORMATION": 0x40,
            "MB_SETFOREGROUND": 0x10000,
        })

        show_about()
        show_about()

        self.assertEqual(2, len(calls))

    def test_dialog_never_runs_until_the_tray_callback_is_invoked(self):
        calls = []
        on_about = InputLifecycleTests._load_app_definition("on_about", {
            "show_about_dialog": lambda: calls.append("shown"),
        })

        self.assertEqual([], calls)

        on_about(mock.Mock(), mock.Mock())

        self.assertEqual(["shown"], calls)

    def test_dialog_failure_is_logged_and_never_propagates_to_the_tray(self):
        logged = []
        message_box = mock.Mock(side_effect=[OSError("no window station"), 1])
        show_about = InputLifecycleTests._load_app_definition("show_about_dialog", {
            "user32": mock.Mock(MessageBoxW=message_box),
            "build_support_snapshot": lambda: "SNAP",
            "format_about": lambda snapshot: "about",
            "append_error": logged.append,
            "DIALOG_LOCK": threading.Lock(),
            "threading": threading,
            "MB_OK": 0x0,
            "MB_ICONINFORMATION": 0x40,
            "MB_SETFOREGROUND": 0x10000,
        })

        show_about()
        show_about()

        self.assertEqual(1, len(logged))
        self.assertIn("About", logged[0])
        self.assertEqual(2, message_box.call_count)

    def test_about_menu_item_is_registered_and_uses_a_dedicated_callback(self):
        source = self._source()

        self.assertTrue(
            'pystray.MenuItem("About / status…", on_about)' in source,
            "tray menu must expose About / status… wired to on_about",
        )

    def test_about_dialog_is_not_invoked_from_the_hid_worker(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)

        worker_names = {
            "start_battery_worker", "publish_battery_snapshot", "worker",
            "hook_thread", "battery_menu_label",
        }
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in worker_names:
                with self.subTest(function=node.name):
                    dumped = ast.dump(node)
                    self.assertNotIn("show_about_dialog", dumped)
                    self.assertNotIn("MessageBoxW", dumped)


class DiagnosticsExportTests(unittest.TestCase):
    def _export(self, directory, **overrides):
        from pearipherals_support import DIAGNOSTICS_FILENAME, diagnostics_payload

        globals_ = {
            "APP_DIR": directory,
            "DIAGNOSTICS_FILENAME": DIAGNOSTICS_FILENAME,
            "diagnostics_payload": diagnostics_payload,
            "save_json_atomic": save_json_atomic,
            "build_support_snapshot": lambda: self._snapshot(),
            "append_error": lambda message: None,
            "_notify": lambda icon, message, title: None,
            "os": os,
            "datetime": __import__("datetime"),
        }
        globals_.update(overrides)
        return InputLifecycleTests._load_app_definition(
            "export_diagnostics", globals_
        )

    @staticmethod
    def _snapshot():
        from pearipherals_support import SupportSnapshot
        from pearipherals_version import APP_VERSION, BuildIdentity

        return SupportSnapshot(
            identity=BuildIdentity(APP_VERSION, "git:" + "b" * 40),
            mode="frozen",
            windows_version="Windows 11",
            windows_build="26100",
            architecture="AMD64",
            config_state="loaded",
            keyboard_battery="available",
            trackpad_battery="not detected",
            raw_input_state="listener registered",
            driver_state="service registration detected",
            gesture_mode="swipes",
            gesture_readiness="ready",
            touchpad_settings="applied",
            scroll_direction="classic",
            autostart="current",
            lifecycle="unchanged",
            removal_readiness="action required",
        )

    def test_nothing_is_written_until_the_callback_is_invoked(self):
        with tempfile.TemporaryDirectory() as directory:
            self._export(directory)

            self.assertEqual([], os.listdir(directory))

    def test_export_writes_a_parseable_report_beside_the_app(self):
        from pearipherals_support import DIAGNOSTICS_FILENAME

        with tempfile.TemporaryDirectory() as directory:
            self._export(directory)(mock.Mock())

            self.assertEqual([DIAGNOSTICS_FILENAME], os.listdir(directory))
            with open(os.path.join(directory, DIAGNOSTICS_FILENAME),
                      encoding="utf-8") as stream:
                payload = json.load(stream)
            self.assertEqual("Pearipherals", payload["product"]["name"])
            self.assertEqual("git:" + "b" * 40, payload["product"]["build_id"])
            self.assertNotIn(directory, json.dumps(payload))

    def test_notification_names_only_the_fixed_filename_never_a_full_path(self):
        from pearipherals_support import DIAGNOSTICS_FILENAME

        notices = []
        with tempfile.TemporaryDirectory() as directory:
            self._export(
                directory,
                _notify=lambda icon, message, title: notices.append((message, title)),
            )(mock.Mock())

        self.assertEqual(1, len(notices))
        message, _title = notices[0]
        self.assertIn(DIAGNOSTICS_FILENAME, message)
        self.assertNotIn(directory, message)
        self.assertNotIn(":\\", message)
        self.assertNotIn("/", message.replace("Pearipherals", ""))

    def test_a_failed_write_notifies_honestly_without_leaking_the_path(self):
        notices = []
        logged = []

        def boom(path, payload):
            raise OSError(rf"denied writing {path}")

        with tempfile.TemporaryDirectory() as directory:
            self._export(
                directory,
                save_json_atomic=boom,
                _notify=lambda icon, message, title: notices.append((message, title)),
                append_error=logged.append,
            )(mock.Mock())

            self.assertEqual([], os.listdir(directory))
            self.assertEqual(1, len(notices))
            message, title = notices[0]
            self.assertNotIn(directory, message)
            self.assertIn("could not", message.lower())
            self.assertEqual(1, len(logged))

    def test_export_menu_item_is_registered_with_a_dedicated_callback(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()

        entry = 'pystray.MenuItem("Save a diagnostic report", on_export_diagnostics)'
        self.assertTrue(entry in source, f"missing tray entry: {entry}")


class RemovalPreparationTests(unittest.TestCase):
    def _callback(self, **overrides):
        from pearipherals_support import (
            format_removal_summary,
            perform_removal,
        )

        recorded = overrides.pop("recorded", [])
        app_config = overrides.pop("app_config", {
            "mac_fkeys": True, "three_finger_mode": "swipes",
        })
        globals_ = {
            "perform_removal": perform_removal,
            "format_removal_summary": format_removal_summary,
            "config": app_config,
            "CONFIG_LOCK": threading.RLock(),
            "CONFIG_RECOVERY_ERROR": None,
            "config_value": lambda key, default=None: app_config.get(key, default),
            "save_config": lambda cfg: recorded.append("save"),
            "autostart_set": lambda enable: recorded.append(f"autostart:{enable}"),
            "tp_settings": mock.Mock(
                restore=lambda: recorded.append("restore") or {}
            ),
            "release_custom_input": lambda abort, disable: (
                recorded.append("release") or []
            ),
            "three_finger_drag": mock.Mock(abort_gesture=lambda: None),
            "suppress_pointer": mock.Mock(set=lambda active: None),
            "confirm_removal": lambda: True,
            "_notify": lambda icon, message, title: recorded.append(("notify", message)),
            "_update_menu": lambda icon: None,
            "append_error": lambda message: recorded.append(("error", message)),
            "os": os,
        }
        globals_.update(overrides)
        callback = InputLifecycleTests._load_app_definition(
            "on_prepare_removal", globals_
        )
        return callback, recorded, app_config

    def test_removal_does_not_restore_native_ownership_after_failed_release(self):
        helper = Phase3InputBlockerTests()
        g, cfg, _, suppression, buttons, _ = helper.setup_gesture()
        helper.start_drag(g)
        cfg.update(tp_settings_backup={"ThreeFingerSlideEnabled": 1},
                   tp_settings_applied=True)
        def failed_button(down):
            buttons.append(down)
            raise OSError("LEFTUP failed")
        g._button = failed_button
        writes = []
        manager = TouchpadSettingsManager(
            cfg, lambda name: 0, lambda name, value: writes.append((name, value)),
            lambda name: None, lambda: None)
        callback, recorded, _ = self._callback(
            app_config=cfg, CONFIG_LOCK=CONFIG_LOCK, three_finger_drag=g,
            suppress_pointer=suppression, release_custom_input=release_custom_input,
            tp_settings=manager)
        callback(None, None)
        self.assertEqual([], writes)
        self.assertEqual({"ThreeFingerSlideEnabled": 1}, cfg["tp_settings_backup"])
        self.assertEqual("off", cfg["three_finger_mode"])
        self.assertFalse(cfg["tp_settings_applied"])
        self.assertIn("autostart:False", recorded)
        suppression.set.assert_called_with(False)
        self.assertIn(g._state, ("dragging", "grace"))
        g._button = buttons.append
        callback(None, None)
        self.assertEqual([("ThreeFingerSlideEnabled", 1)], writes)
        self.assertEqual({}, cfg["tp_settings_backup"])
        self.assertEqual("idle", g._state)

    def test_confirmed_removal_disables_settings_and_restores_state(self):
        callback, recorded, app_config = self._callback()

        callback(mock.Mock(), mock.Mock())

        self.assertFalse(app_config["mac_fkeys"])
        self.assertEqual("off", app_config["three_finger_mode"])
        self.assertIn("autostart:False", recorded)
        self.assertIn("restore", recorded)
        self.assertIn("release", recorded)

    def test_cancellation_performs_absolutely_no_mutation(self):
        callback, recorded, app_config = self._callback(
            confirm_removal=lambda: False
        )
        before = dict(app_config)

        callback(mock.Mock(), mock.Mock())

        self.assertEqual(before, app_config)
        self.assertEqual([], recorded)

    def test_recovery_required_config_blocks_mutation_and_explains_manually(self):
        notices = []
        callback, recorded, app_config = self._callback(
            CONFIG_RECOVERY_ERROR=OSError(r"bad config at D:\Portable\p.json"),
            _notify=lambda icon, message, title: notices.append((message, title)),
        )
        before = dict(app_config)

        callback(mock.Mock(), mock.Mock())

        self.assertEqual(before, app_config)
        self.assertNotIn("autostart:False", recorded)
        self.assertNotIn("restore", recorded)
        self.assertEqual(1, len(notices))
        message, _title = notices[0]
        self.assertNotIn("Portable", message)
        self.assertIn("Start with Windows", message)

    def test_a_failed_step_never_reports_success_but_still_runs_the_rest(self):
        notices = []

        def failing_autostart(enable):
            raise OSError(r"registry denied at HKCU\...\Run")

        callback, recorded, _config = self._callback(
            autostart_set=failing_autostart,
            _notify=lambda icon, message, title: notices.append((message, title)),
        )

        callback(mock.Mock(), mock.Mock())

        self.assertIn("restore", recorded)
        self.assertEqual(1, len(notices))
        message, _title = notices[0]
        self.assertIn("could not be completed", message)
        self.assertNotIn("denied", message)
        self.assertNotIn("HKCU", message)

    def test_removal_never_deletes_files_or_stops_the_application(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "on_prepare_removal"
        )
        dumped = ast.dump(node)

        for forbidden in (
            "remove", "unlink", "rmtree", "_exit", "icon.stop", "shutdown_custom_input",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, dumped)

    def test_confirmation_dialog_is_ownerless_defaults_to_no_and_allows_cancel(self):
        calls = []
        confirm = InputLifecycleTests._load_app_definition("confirm_removal", {
            "user32": mock.Mock(MessageBoxW=lambda *a: calls.append(a) or 7),
            "append_error": lambda message: None,
            "DIALOG_LOCK": threading.Lock(),
            "threading": threading,
            "MB_YESNOCANCEL": 0x3,
            "MB_ICONWARNING": 0x30,
            "MB_DEFBUTTON2": 0x100,
            "MB_SETFOREGROUND": 0x10000,
            "IDYES": 6,
        })

        self.assertFalse(confirm())

        self.assertEqual(1, len(calls))
        hwnd, text, _title, flags = calls[0]
        self.assertIsNone(hwnd)
        self.assertEqual(0x3 | 0x30 | 0x100 | 0x10000, flags)
        self.assertIn("Choose No, Cancel, or X to leave everything as it is.", text)

    def test_only_explicit_yes_authorizes_removal(self):
        for answer, expected in (
            (6, True),
            (7, False),
            (2, False),
            (0, False),
            (999, False),
        ):
            with self.subTest(answer=answer):
                confirm = InputLifecycleTests._load_app_definition(
                    "confirm_removal",
                    {
                        "user32": mock.Mock(MessageBoxW=lambda *args, a=answer: a),
                        "append_error": lambda message: None,
                        "DIALOG_LOCK": threading.Lock(),
                        "threading": threading,
                        "MB_YESNOCANCEL": 0x3,
                        "MB_ICONWARNING": 0x30,
                        "MB_DEFBUTTON2": 0x100,
                        "MB_SETFOREGROUND": 0x10000,
                        "IDYES": 6,
                    },
                )

                self.assertIs(expected, confirm())

    def test_confirmation_message_box_runs_off_the_pystray_callback_thread(self):
        caller_thread = threading.get_ident()
        dialog_threads = []
        confirm = InputLifecycleTests._load_app_definition("confirm_removal", {
            "user32": mock.Mock(
                MessageBoxW=lambda *args: dialog_threads.append(
                    threading.get_ident()
                ) or 7
            ),
            "append_error": lambda message: None,
            "DIALOG_LOCK": threading.Lock(),
            "threading": threading,
            "MB_YESNOCANCEL": 0x3,
            "MB_ICONWARNING": 0x30,
            "MB_DEFBUTTON2": 0x100,
            "MB_SETFOREGROUND": 0x10000,
            "IDYES": 6,
        })

        self.assertFalse(confirm())

        self.assertEqual(1, len(dialog_threads))
        self.assertNotEqual(caller_thread, dialog_threads[0])

    def test_confirmation_blocks_reentrant_stack_and_fails_closed(self):
        calls = []
        confirm = None

        def message_box(*args):
            calls.append(args)
            if len(calls) == 1:
                self.assertFalse(confirm())
            return 7

        confirm = InputLifecycleTests._load_app_definition("confirm_removal", {
            "user32": mock.Mock(MessageBoxW=message_box),
            "append_error": lambda message: None,
            "DIALOG_LOCK": threading.Lock(),
            "threading": threading,
            "MB_YESNOCANCEL": 0x3,
            "MB_ICONWARNING": 0x30,
            "MB_DEFBUTTON2": 0x100,
            "MB_SETFOREGROUND": 0x10000,
            "IDYES": 6,
        })

        self.assertFalse(confirm())
        self.assertFalse(confirm())

        self.assertEqual(2, len(calls))

    def test_confirmation_prototypes_and_flags_are_declared_explicitly(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()

        for declaration in (
            "user32.MessageBoxW.argtypes = (w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT)",
            "user32.MessageBoxW.restype = ctypes.c_int",
            "MB_YESNOCANCEL = 0x00000003",
            "MB_ICONWARNING = 0x00000030",
            "MB_DEFBUTTON2 = 0x00000100",
            "MB_SETFOREGROUND = 0x00010000",
            "IDYES = 6",
        ):
            with self.subTest(declaration=declaration):
                self.assertTrue(
                    declaration in source,
                    f"missing declaration: {declaration}",
                )

    def test_a_failed_confirmation_dialog_cancels_instead_of_mutating(self):
        message_box = mock.Mock(side_effect=[OSError("no window station"), 7])
        confirm = InputLifecycleTests._load_app_definition("confirm_removal", {
            "user32": mock.Mock(MessageBoxW=message_box),
            "append_error": lambda message: None,
            "DIALOG_LOCK": threading.Lock(),
            "threading": threading,
            "MB_YESNOCANCEL": 0x3,
            "MB_ICONWARNING": 0x30,
            "MB_DEFBUTTON2": 0x100,
            "MB_SETFOREGROUND": 0x10000,
            "IDYES": 6,
        })

        self.assertFalse(confirm())
        self.assertFalse(confirm())
        self.assertEqual(2, message_box.call_count)

    def test_removal_menu_item_is_registered_with_a_dedicated_callback(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()

        entry = 'pystray.MenuItem("Prepare for removal…", on_prepare_removal)'
        self.assertTrue(entry in source, f"missing tray entry: {entry}")


class BuildIdentityResolutionTests(unittest.TestCase):
    """A one-file PyInstaller build extracts bundled data to sys._MEIPASS,
    which is NOT the directory holding the executable. Reading only APP_DIR
    would silently degrade every frozen build to the unknown build id."""

    def _identity(self, directory, meipass=None):
        from pearipherals_support import DIAGNOSTICS_FILENAME  # noqa: F401
        from pearipherals_version import APP_VERSION, BuildIdentity

        fake_sys = mock.Mock(frozen=True)
        if meipass is None:
            del fake_sys._MEIPASS
        else:
            fake_sys._MEIPASS = meipass
        return InputLifecycleTests._load_app_definition("build_identity", {
            "IS_FROZEN": True,
            "APP_DIR": directory,
            "BUILD_MANIFEST_NAME": "pearipherals-build.json",
            "UNKNOWN_BUILD_ID": "git:" + "0" * 40,
            "APP_VERSION": APP_VERSION,
            "BuildIdentity": BuildIdentity,
            "entry_source_build_id": lambda path: "entry-sha256:" + "f" * 64,
            "json": json,
            "os": os,
            "sys": fake_sys,
        })

    @staticmethod
    def _write_manifest(directory, build_id, version="1.2.0"):
        with open(os.path.join(directory, "pearipherals-build.json"), "w",
                  encoding="utf-8") as stream:
            json.dump({"build_id": build_id, "version": version}, stream)

    def test_one_file_build_reads_the_manifest_from_the_extraction_dir(self):
        with tempfile.TemporaryDirectory() as exe_dir, \
                tempfile.TemporaryDirectory() as meipass:
            self._write_manifest(meipass, "git:" + "a" * 40)

            identity = self._identity(exe_dir, meipass=meipass)()

            self.assertEqual("git:" + "a" * 40, identity.build_id)

    def test_frozen_runtime_version_comes_from_the_validated_manifest(self):
        with tempfile.TemporaryDirectory() as exe_dir, \
                tempfile.TemporaryDirectory() as meipass:
            self._write_manifest(meipass, "git:" + "b" * 40, version="9.8.7")

            identity = self._identity(exe_dir, meipass=meipass)()

            self.assertEqual("9.8.7", identity.version)
            self.assertEqual("git:" + "b" * 40, identity.build_id)

    def test_a_manifest_beside_the_executable_still_works(self):
        with tempfile.TemporaryDirectory() as exe_dir:
            self._write_manifest(exe_dir, "git:" + "c" * 40)

            identity = self._identity(exe_dir)()

            self.assertEqual("git:" + "c" * 40, identity.build_id)

    def test_invalid_embedded_manifest_never_falls_back_to_adjacent_manifest(self):
        with tempfile.TemporaryDirectory() as exe_dir, \
                tempfile.TemporaryDirectory() as meipass:
            self._write_manifest(meipass, "not-a-build-id")
            self._write_manifest(exe_dir, "git:" + "e" * 40)

            identity = self._identity(exe_dir, meipass=meipass)()

            from pearipherals_version import APP_VERSION

            self.assertEqual("git:" + "0" * 40, identity.build_id)
            self.assertEqual(APP_VERSION, identity.version)

    def test_present_falsey_meipass_never_reads_adjacent_or_working_directory(self):
        with tempfile.TemporaryDirectory() as exe_dir, \
                tempfile.TemporaryDirectory() as working_dir:
            self._write_manifest(exe_dir, "git:" + "e" * 40)
            self._write_manifest(working_dir, "git:" + "f" * 40)
            previous_directory = os.getcwd()
            try:
                from pearipherals_version import APP_VERSION

                os.chdir(working_dir)
                root_relative = os.path.splitdrive(working_dir)[1]
                for meipass in ("", root_relative):
                    with self.subTest(meipass=meipass):
                        identity = self._identity(exe_dir, meipass=meipass)()
                        self.assertEqual("git:" + "0" * 40, identity.build_id)
                        self.assertEqual(APP_VERSION, identity.version)
            finally:
                os.chdir(previous_directory)

    def test_a_missing_or_invalid_manifest_degrades_to_the_unknown_identity(self):
        with tempfile.TemporaryDirectory() as exe_dir:
            self.assertEqual(
                "git:" + "0" * 40, self._identity(exe_dir)().build_id
            )

            self._write_manifest(exe_dir, "not-a-build-id")
            self.assertEqual(
                "git:" + "0" * 40, self._identity(exe_dir)().build_id
            )

    def test_the_manifest_never_reaches_the_surface_as_a_path(self):
        with tempfile.TemporaryDirectory() as exe_dir, \
                tempfile.TemporaryDirectory() as meipass:
            self._write_manifest(meipass, "git:" + "d" * 40)

            identity = self._identity(exe_dir, meipass=meipass)()

            self.assertNotIn(meipass, str(identity))
            self.assertNotIn(exe_dir, str(identity))


class SupportLinkTests(unittest.TestCase):
    @staticmethod
    def _constants():
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
        return constants

    def test_support_links_are_fixed_https_project_constants(self):
        constants = self._constants()

        self.assertEqual(
            "https://github.com/OnlineFix/Pearipherals#readme",
            constants["DOCS_URL"],
        )
        self.assertEqual(
            "https://github.com/OnlineFix/Pearipherals/issues/new/choose",
            constants["REPORT_URL"],
        )
        for url in (constants["DOCS_URL"], constants["REPORT_URL"]):
            with self.subTest(url=url):
                self.assertTrue(url.startswith("https://"))
                self.assertNotIn("{", url)
                self.assertNotIn("%s", url)

    def test_documentation_link_opens_only_when_the_callback_is_invoked(self):
        opened = []
        on_docs = InputLifecycleTests._load_app_definition("on_open_docs", {
            "open_support_link": opened.append,
            "DOCS_URL": "https://example.invalid/docs",
        })

        self.assertEqual([], opened)

        on_docs(mock.Mock(), mock.Mock())

        self.assertEqual(["https://example.invalid/docs"], opened)

    def test_report_link_opens_only_when_the_callback_is_invoked(self):
        opened = []
        on_report = InputLifecycleTests._load_app_definition("on_report_problem", {
            "open_support_link": opened.append,
            "REPORT_URL": "https://example.invalid/issues",
        })

        self.assertEqual([], opened)

        on_report(mock.Mock(), mock.Mock())

        self.assertEqual(["https://example.invalid/issues"], opened)

    def test_link_opener_rejects_anything_that_is_not_a_fixed_project_url(self):
        opened = []
        logged = []
        open_support_link = InputLifecycleTests._load_app_definition(
            "open_support_link",
            {
                "webbrowser": mock.Mock(open=opened.append),
                "append_error": logged.append,
                "DOCS_URL": "https://github.com/OnlineFix/Pearipherals#readme",
                "REPORT_URL":
                    "https://github.com/OnlineFix/Pearipherals/issues/new/choose",
            },
        )

        for hostile in (
            "http://github.com/OnlineFix/Pearipherals",
            "file:///C:/Users/name/secret.txt",
            "https://evil.invalid/",
            r"\\server\share\payload.exe",
        ):
            with self.subTest(url=hostile):
                open_support_link(hostile)

        self.assertEqual([], opened)

        open_support_link("https://github.com/OnlineFix/Pearipherals#readme")

        self.assertEqual(
            ["https://github.com/OnlineFix/Pearipherals#readme"], opened
        )

    def test_browser_failure_is_logged_without_leaking_local_paths(self):
        logged = []
        open_support_link = InputLifecycleTests._load_app_definition(
            "open_support_link",
            {
                "webbrowser": mock.Mock(
                    open=mock.Mock(
                        side_effect=OSError(r"no handler at C:\Users\name\browser")
                    )
                ),
                "append_error": logged.append,
                "DOCS_URL": "https://github.com/OnlineFix/Pearipherals#readme",
                "REPORT_URL":
                    "https://github.com/OnlineFix/Pearipherals/issues/new/choose",
            },
        )

        open_support_link("https://github.com/OnlineFix/Pearipherals#readme")

        self.assertEqual(1, len(logged))
        self.assertNotIn("C:\\Users\\name", logged[0])

    def test_no_network_access_happens_at_import_time(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            with self.subTest(node=type(node).__name__):
                dumped = ast.dump(node)
                for network in ("webbrowser.open", "urlopen", "requests", "socket"):
                    self.assertNotIn(network, dumped)

    def test_support_link_menu_items_are_registered(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()

        for entry in (
            'pystray.MenuItem("Open documentation", on_open_docs)',
            'pystray.MenuItem("Report a problem", on_report_problem)',
        ):
            with self.subTest(entry=entry):
                self.assertTrue(entry in source, f"missing tray entry: {entry}")


class AutostartMigrationTests(unittest.TestCase):
    """Legacy Run cleanup must never delete before the current value exists."""

    def _startup_migration(self, winreg_stub, command='"D:\\App\\P.exe"'):
        from pearipherals_support import classify_autostart

        return InputLifecycleTests._load_app_definition(
            "migrate_legacy_autostart",
            {
                "winreg": winreg_stub,
                "RUN_KEY": "Software\\Fake\\Run",
                "RUN_NAME": "Pearipherals",
                "OLD_RUN_NAMES": ("MagicKeys", "MagicSuite"),
                "classify_autostart": classify_autostart,
                "autostart_command": lambda: command,
                "append_error": lambda message: None,
            },
        )

    def test_nothing_is_deleted_when_no_legacy_entry_exists(self):
        stub = AutostartProbeTests.FakeWinreg(values={})

        self._startup_migration(stub)()

        self.assertEqual([], stub.deleted)
        self.assertEqual([], stub.written)

    def test_current_value_is_written_and_read_back_before_any_deletion(self):
        stub = AutostartProbeTests.FakeWinreg(
            values={"MagicSuite": '"D:\\Old\\MagicSuite.exe"'}
        )

        self._startup_migration(stub)()

        self.assertEqual([("Pearipherals", '"D:\\App\\P.exe"')], stub.written)
        self.assertEqual(["MagicSuite"], stub.deleted)
        self.assertNotIn("MagicSuite", stub.values)
        self.assertEqual('"D:\\App\\P.exe"', stub.values["Pearipherals"])

    def test_a_failed_replacement_leaves_every_legacy_entry_intact(self):
        legacy = {
            "MagicKeys": '"D:\\Old\\MagicKeys.exe"',
            "MagicSuite": '"D:\\Old\\MagicSuite.exe"',
        }

        class RefusingWrite(AutostartProbeTests.FakeWinreg):
            def SetValueEx(self, key, name, reserved, kind, value):
                raise PermissionError(5, "access denied")

        stub = RefusingWrite(values=dict(legacy))

        self._startup_migration(stub)()

        self.assertEqual([], stub.deleted)
        self.assertEqual(legacy, stub.values)

    def test_an_unverifiable_readback_leaves_every_legacy_entry_intact(self):
        class LyingReadback(AutostartProbeTests.FakeWinreg):
            def QueryValueEx(self, key, name):
                return ('"D:\\Somewhere\\Else.exe"', self.REG_SZ)

        stub = LyingReadback(values={"MagicSuite": '"D:\\Old\\MagicSuite.exe"'})

        self._startup_migration(stub)()

        self.assertEqual([], stub.deleted)
        self.assertIn("MagicSuite", stub.values)

    def test_partial_cleanup_stays_observable_and_retryable(self):
        class DenyOneDelete(AutostartProbeTests.FakeWinreg):
            def DeleteValue(self, key, name):
                if name == "MagicKeys":
                    self.deleted.append(name)
                    raise PermissionError(5, "access denied")
                return super().DeleteValue(key, name)

        stub = DenyOneDelete(
            values={
                "MagicKeys": '"D:\\Old\\MagicKeys.exe"',
                "MagicSuite": '"D:\\Old\\MagicSuite.exe"',
            }
        )

        self._startup_migration(stub)()

        self.assertEqual(["MagicKeys", "MagicSuite"], stub.deleted)
        self.assertIn("MagicKeys", stub.values)
        self.assertNotIn("MagicSuite", stub.values)

    def test_startup_calls_migration_before_first_run_setup(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            source = stream.read()

        main_body = source.split("def main():", 1)[1]
        self.assertLess(
            main_body.index("migrate_legacy_autostart()"),
            main_body.index("first_run_setup()"),
        )
        self.assertNotIn("winreg.DeleteValue(k, old)", source)


class AutostartRemovalReadbackTests(unittest.TestCase):
    def _autostart_set(self, winreg_stub, command='"D:\\App\\P.exe"'):
        from pearipherals_support import classify_autostart

        return InputLifecycleTests._load_app_definition(
            "autostart_set",
            {
                "winreg": winreg_stub,
                "RUN_KEY": "Software\\Fake\\Run",
                "RUN_NAME": "Pearipherals",
                "OLD_RUN_NAMES": ("MagicKeys", "MagicSuite"),
                "classify_autostart": classify_autostart,
                "autostart_command": lambda: command,
            },
        )

    def test_disable_verifies_every_name_is_actually_gone(self):
        stub = AutostartProbeTests.FakeWinreg(
            values={
                "Pearipherals": '"D:\\App\\P.exe"',
                "MagicSuite": '"D:\\Old\\MagicSuite.exe"',
            }
        )

        self._autostart_set(stub)(False)

        self.assertEqual({}, stub.values)
        self.assertEqual(["Pearipherals", "MagicKeys", "MagicSuite"], stub.deleted)

    def test_a_value_that_survives_deletion_is_reported_not_claimed_removed(self):
        class Undeletable(AutostartProbeTests.FakeWinreg):
            def DeleteValue(self, key, name):
                self.deleted.append(name)  # reports success but changes nothing

        stub = Undeletable(values={"Pearipherals": '"D:\\App\\P.exe"'})

        with self.assertRaises(OSError):
            self._autostart_set(stub)(False)


class AdoptedInstallOnboardingTests(unittest.TestCase):
    def test_adopting_a_legacy_config_does_not_trigger_first_run_mutation(self):
        from pearipherals_core import load_json_config

        with tempfile.TemporaryDirectory() as directory:
            legacy_path = os.path.join(directory, "magicsuite.json")
            with open(legacy_path, "w", encoding="utf-8") as stream:
                json.dump({"cfg_version": 5, "mac_fkeys": False}, stream)

            saved = {}
            load_config = InputLifecycleTests._load_app_definition(
                "load_config",
                {
                    "CONFIG_LOCK": threading.RLock(),
                    "CONFIG_PATH": os.path.join(directory, "pearipherals.json"),
                    "OLD_CONFIG_PATHS": ((legacy_path, "MagicSuite"),),
                    "DEFAULTS": {"cfg_version": 6},
                    "load_json_config": load_json_config,
                    "ConfigRecoveryRequired": Exception,
                    "save_config": saved.update,
                },
            )

            adopted = load_config()

            self.assertEqual("MagicSuite", adopted["legacy_source"])
            self.assertTrue(
                adopted["setup_done"],
                "an adopted install is already onboarded; first_run_setup must "
                "not re-apply autostart and touchpad registry changes",
            )


class SnippingKeyboardHookTests(unittest.TestCase):
    @staticmethod
    def _load_hook(modifier_down=False, mac_fkeys=True):
        source_path = os.path.join(os.path.dirname(__file__), "..", "pearipherals.py")
        with open(source_path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), source_path)

        action_table = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "ACTIONS"
                    for target in node.targets)
        )
        keyboard_struct = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "KBDLLHOOKSTRUCT"
        )
        hook = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "ll_hook"
        )

        control = {
            "modifier_down": modifier_down,
            "mac_fkeys": mac_fkeys,
        }

        class FakeUser32:
            def __init__(self):
                self.forwarded = []

            @staticmethod
            def GetAsyncKeyState(_vk):
                return 0x8000 if control["modifier_down"] else 0

            def CallNextHookEx(self, *args):
                self.forwarded.append(args)
                return 9876

        import queue
        namespace = {
            "ctypes": ctypes,
            "w": wintypes,
            "ULONG_PTR": ctypes.c_size_t,
            "HOOKPROC": lambda function: function,
            "user32": FakeUser32(),
            "config_value": lambda name: (
                control["mac_fkeys"] if name == "mac_fkeys" else 10
            ),
            "actions": queue.Queue(),
            "_control": control,
            "VK_LWIN": 0x5B,
            "VK_TAB": 0x09,
            "VK_S": 0x53,
            "VK_MEDIA_PREV": 0xB1,
            "VK_MEDIA_PLAY": 0xB3,
            "VK_MEDIA_NEXT": 0xB0,
            "VK_VOL_MUTE": 0xAD,
            "VK_VOL_DOWN": 0xAE,
            "VK_VOL_UP": 0xAF,
            "VK_F1": 0x70,
            "VK_F6": 0x75,
            "VK_F12": 0x7B,
            "MOD_VKS": (0x10, 0x11, 0x12, 0x5B, 0x5C),
            "WM_KEYDOWN": 0x0100,
            "WM_SYSKEYDOWN": 0x0104,
            "WM_KEYUP": 0x0101,
            "WM_SYSKEYUP": 0x0105,
            "SNIP_KEY_STATE": "up",
            "LLKHF_INJECTED": 0x10,
            "MAGIC_EXTRA": 0x50454152,
        }
        exec(compile(ast.Module([action_table, keyboard_struct, hook], []),
                     source_path, "exec"), namespace)
        return namespace

    @staticmethod
    def _invoke(namespace, vk, w_param=0x0100, flags=0, extra_info=0):
        event = namespace["KBDLLHOOKSTRUCT"]()
        event.vkCode = vk
        event.flags = flags
        event.dwExtraInfo = extra_info
        return namespace["ll_hook"](
            0, w_param, ctypes.addressof(event)
        )

    def test_unmodified_f6_enqueues_snipping_overlay_and_is_swallowed(self):
        namespace = self._load_hook()

        result = self._invoke(namespace, 0x75)

        self.assertEqual(1, result)
        self.assertEqual(("snip", None), namespace["actions"].get_nowait())
        self.assertEqual([], namespace["user32"].forwarded)

    def test_modifier_f6_remains_passthrough(self):
        namespace = self._load_hook(modifier_down=True)

        result = self._invoke(namespace, 0x75)

        self.assertEqual(9876, result)
        self.assertTrue(namespace["actions"].empty())
        self.assertEqual(1, len(namespace["user32"].forwarded))

    def test_modifier_f6_press_stays_passthrough_after_modifier_release(self):
        namespace = self._load_hook(modifier_down=True)

        self.assertEqual(9876, self._invoke(namespace, 0x75, 0x0100))
        namespace["_control"]["modifier_down"] = False
        self.assertEqual(9876, self._invoke(namespace, 0x75, 0x0100))
        self.assertEqual(9876, self._invoke(namespace, 0x75, 0x0101))

        self.assertTrue(namespace["actions"].empty())
        self.assertEqual(3, len(namespace["user32"].forwarded))

    def test_keyup_resets_captured_press_even_while_frow_is_disabled(self):
        namespace = self._load_hook()

        self.assertEqual(1, self._invoke(namespace, 0x75, 0x0100))
        namespace["_control"]["mac_fkeys"] = False
        self.assertEqual(1, self._invoke(namespace, 0x75, 0x0101))
        namespace["_control"]["mac_fkeys"] = True
        self.assertEqual(1, self._invoke(namespace, 0x75, 0x0100))

        self.assertEqual(
            [("snip", None), ("snip", None)],
            [namespace["actions"].get_nowait(), namespace["actions"].get_nowait()],
        )
        self.assertTrue(namespace["actions"].empty())

    def test_injected_f6_never_changes_physical_press_state(self):
        namespace = self._load_hook()

        self.assertEqual(
            9876,
            self._invoke(namespace, 0x75, 0x0100, flags=0x10),
        )
        self.assertEqual(1, self._invoke(namespace, 0x75, 0x0100))

        self.assertEqual(("snip", None), namespace["actions"].get_nowait())
        self.assertTrue(namespace["actions"].empty())

    def test_app_tagged_f6_never_changes_physical_press_state(self):
        namespace = self._load_hook()

        self.assertEqual(
            9876,
            self._invoke(
                namespace,
                0x75,
                0x0100,
                extra_info=namespace["MAGIC_EXTRA"],
            ),
        )
        self.assertEqual(1, self._invoke(namespace, 0x75, 0x0100))

        self.assertEqual(("snip", None), namespace["actions"].get_nowait())
        self.assertTrue(namespace["actions"].empty())

    def test_frow_off_passes_the_complete_f6_press_through(self):
        namespace = self._load_hook(mac_fkeys=False)

        self.assertEqual(9876, self._invoke(namespace, 0x75, 0x0100))
        self.assertEqual(9876, self._invoke(namespace, 0x75, 0x0100))
        self.assertEqual(9876, self._invoke(namespace, 0x75, 0x0101))

        self.assertTrue(namespace["actions"].empty())
        self.assertEqual(3, len(namespace["user32"].forwarded))

    def test_f6_typematic_repeat_is_suppressed_until_keyup(self):
        namespace = self._load_hook()

        self._invoke(namespace, 0x75, 0x0100)
        self._invoke(namespace, 0x75, 0x0100)
        self._invoke(namespace, 0x75, 0x0101)
        self._invoke(namespace, 0x75, 0x0100)

        self.assertEqual(
            [("snip", None), ("snip", None)],
            [namespace["actions"].get_nowait(), namespace["actions"].get_nowait()],
        )
        self.assertTrue(namespace["actions"].empty())


if __name__ == "__main__":
    unittest.main()
