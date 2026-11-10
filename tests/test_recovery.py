from copy import deepcopy
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from reincarnation.capture_profile import ReplayInput
from reincarnation.config import defaults, load, save, validate
from reincarnation.engine import Engine
from reincarnation.recovery import Recovery
from reincarnation.vision import Observation

if sys.platform == "win32":
    from reincarnation.worker import Worker
    from reincarnation import worker as worker_module


class RecoveryTests(unittest.TestCase):
    def test_consecutive_errors_exhaust_budget_and_manual_start_resets(self):
        recovery = Recovery(defaults()["recovery"])
        self.assertFalse(recovery.failure())  # no user start yet
        recovery.activate()
        for index in range(3):
            self.assertTrue(recovery.failure())
            self.assertTrue(recovery.reserve())
            self.assertEqual(recovery.attempts, index + 1)
        self.assertFalse(recovery.failure())
        self.assertFalse(recovery.reserve())
        recovery.activate()
        self.assertTrue(recovery.failure())

    def test_budget_requires_continuous_confirmed_healthy_frames_to_reset(self):
        recovery = Recovery(defaults()["recovery"])
        recovery.activate()
        recovery.failure()
        recovery.reserve()
        recovery.healthy(0, True)
        recovery.healthy(119, True)
        self.assertEqual(recovery.attempts, 1)
        recovery.healthy(120, False)  # unknown frame interrupts healthy period
        recovery.healthy(121, True)
        recovery.healthy(240, True)
        self.assertEqual(recovery.attempts, 1)
        recovery.healthy(241, True)
        self.assertEqual(recovery.attempts, 0)

    def test_cancel_and_disabled_recovery_do_not_restart(self):
        recovery = Recovery(defaults()["recovery"])
        recovery.activate()
        recovery.failure()
        recovery.cancel()
        self.assertFalse(recovery.reserve())
        self.assertFalse(recovery.failure())
        recovery.cfg["enabled"] = False
        recovery.activate()
        self.assertFalse(recovery.failure())

    def test_config_validation_and_older_profile_migration(self):
        cfg = defaults()
        for key, bad in (("max_restarts", 0), ("max_restarts", True), ("stable_seconds", 0), ("enabled", "yes")):
            invalid = deepcopy(cfg)
            invalid["recovery"][key] = bad
            with self.assertRaises(ValueError):
                validate(invalid)
        with tempfile.TemporaryDirectory() as folder:
            import json
            cfg.pop("recovery")
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            self.assertTrue(load(path)["recovery"]["enabled"])


class FakeInput(ReplayInput):
    def __init__(self, spec, max_frame_age):
        super().__init__()
        self.spec, self.max_frame_age = spec, max_frame_age
        self.lock = threading.RLock()
        self.enabled = False
        self.target = None
        self.frame_at = time.monotonic()

    def arm(self):
        with self.lock:
            self.enabled = True
            self.target = None
            self.frame_at = time.monotonic()

    def inhibit(self):
        with self.lock:
            self.enabled = False
            self.release_all()

    def frame(self, target, at):
        self.target, self.frame_at = target, at

    def flush(self, now):
        pass


@unittest.skipUnless(sys.platform == "win32", "Windows worker")
class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        cfg = defaults()
        save(self.root / "settings.json", cfg)
        self.worker = Worker.__new__(Worker)
        w = self.worker
        w.root, w.config_path, w.cfg = self.root, self.root / "settings.json", cfg
        w.events, w.commands = queue.Queue(), queue.Queue()
        w.shutdown, w.interrupt, w.manual_stop = threading.Event(), threading.Event(), threading.Event()
        w.control_lock, w.control_generation = threading.RLock(), 0
        w.output = FakeInput(cfg["window"], 1.5)
        w.output.arm()
        w.log = Mock()
        w.engine = Engine(cfg, w.output, w.log)
        w.engine.start(0)
        w.last_obs = Observation("match", 1)
        w.last_frame = None
        w.recovery = Recovery(cfg["recovery"])
        w.recovery.activate()
        w._diagnostic = Mock()
        self.focus = patch.object(worker_module, "foreground", return_value=object())
        self.focus_mock = self.focus.start()
        self.addCleanup(self.focus.stop)

    def test_failure_releases_then_restarts_without_delay_and_preserves_cycles(self):
        w = self.worker
        w.engine.cycles = 5
        w.output.down("W")
        w._fault("Synthetic failure")
        self.assertFalse(w.output.keys)
        self.assertFalse(w.output.enabled)
        self.assertTrue(w._try_recover())
        self.assertTrue(w.engine.running)
        self.assertTrue(w.output.enabled)
        self.assertEqual(w.engine.state, "unknown")
        self.assertEqual(w.last_obs.state, "unknown")
        self.assertEqual(w.engine.cycles, 5)

    def test_focus_wait_does_not_consume_budget_then_recovers(self):
        w = self.worker
        w._fault("Game lost focus")
        self.focus_mock.return_value = None
        for _ in range(10):
            self.assertFalse(w._try_recover())
        self.assertEqual(w.recovery.attempts, 0)
        self.assertFalse(w.output.enabled)
        self.focus_mock.return_value = object()
        self.assertTrue(w._try_recover())

    def test_manual_stop_cancels_pending_restart_before_queue_is_processed(self):
        w = self.worker
        w._fault("Synthetic failure")
        w.request("stop")
        self.assertFalse(w._try_recover())
        self.assertFalse(w.output.enabled)
        self.assertFalse(w.engine.running)

    def test_stop_during_config_reload_prevents_rearming(self):
        w = self.worker
        w._fault("Synthetic failure")
        real_load = worker_module.load

        def stop_during_load(path):
            cfg = real_load(path)
            w.request("stop")
            return cfg

        with patch.object(worker_module, "load", side_effect=stop_during_load):
            self.assertFalse(w._try_recover())
        self.assertFalse(w.output.enabled)
        self.assertFalse(w.engine.running)

    def test_permanent_start_failure_stops_after_three_attempts(self):
        w = self.worker
        w._fault("Synthetic failure")
        with patch.object(w, "_reload_start", side_effect=ValueError("broken profile")) as restart:
            for _ in range(10):
                w._try_recover()
            self.assertEqual(restart.call_count, 3)
        self.assertFalse(w.recovery.pending)
        self.assertFalse(w.engine.running)

    def test_failing_focus_api_is_bounded_without_killing_controller(self):
        w = self.worker
        w._fault("Synthetic failure")
        self.focus_mock.side_effect = OSError("window query failed")
        for _ in range(10):
            self.assertFalse(w._try_recover())
        self.assertEqual(w.recovery.attempts, 3)
        self.assertFalse(w.recovery.pending)

    def test_snapshot_request_cancels_recovery_and_older_queued_start(self):
        w = self.worker
        w._fault("Synthetic failure")
        w.request("start")
        _, stale_generation = w.commands.get_nowait()
        w.request("capture")
        self.assertNotEqual(stale_generation, w.control_generation)
        self.assertFalse(w._try_recover())
        self.assertFalse(w.output.enabled)

    def test_thread_recovers_from_capture_exception_and_respects_stop(self):
        # Run the real worker/watchdog loops with every input and capture mocked.
        self.focus_mock.return_value = object()
        with patch.object(worker_module, "Input", FakeInput), \
             patch.object(worker_module, "capture", side_effect=[RuntimeError("capture failed")] + [Image.new("RGB", (200, 100))] * 100):
            w = Worker(self.root, queue.Queue())
            try:
                w.request("start")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not (w.recovery.attempts == 1 and w.engine.running):
                    time.sleep(.01)
                self.assertEqual(w.recovery.attempts, 1)
                self.assertTrue(w.engine.running)
                w.request("stop")
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and w.engine.running:
                    time.sleep(.01)
                self.assertFalse(w.engine.running)
                self.assertFalse(w.output.enabled)
                self.assertTrue(w.manual_stop.is_set())
            finally:
                w.close()

    def test_thread_recovers_from_automatic_engine_pause(self):
        original_tick = Engine.tick
        failed = threading.Event()

        def pause_once(engine, obs, focused, now):
            if not failed.is_set():
                failed.set()
                engine.pause("Synthetic state timeout")
            else:
                original_tick(engine, obs, focused, now)

        with patch.object(worker_module, "Input", FakeInput), \
             patch.object(worker_module, "capture", return_value=Image.new("RGB", (200, 100))), \
             patch.object(Engine, "tick", new=pause_once):
            w = Worker(self.root, queue.Queue())
            try:
                w.request("start")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not (w.recovery.attempts == 1 and w.engine.running):
                    time.sleep(.01)
                self.assertTrue(failed.is_set())
                self.assertEqual(w.recovery.attempts, 1)
                self.assertTrue(w.engine.running)
            finally:
                w.close()


if __name__ == "__main__":
    unittest.main()
