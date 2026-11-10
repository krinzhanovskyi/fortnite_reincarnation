import ctypes
import sys
import time
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    from reincarnation import windows as win


@unittest.skipUnless(sys.platform == "win32", "Windows input backend")
class WindowsInputTests(unittest.TestCase):
    def setUp(self):
        self.target = win.Target(123, (0, 0, 1920, 1080), "Fortnite", "game.exe")
        self.output = win.Input({"process_names": ["game.exe"], "title_contains": "Fortnite"}, 1)
        self.focus = patch.object(win, "foreground", return_value=self.target)
        self.focus_mock = self.focus.start()
        self.send = patch.object(win, "_send")
        self.send_mock = self.send.start()
        self.addCleanup(self.focus.stop)
        self.addCleanup(self.send.stop)
        self.output.arm()
        self.output.frame(self.target, time.monotonic())

    def test_native_input_structure_size(self):
        self.assertEqual(ctypes.sizeof(win.INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_focus_checked_before_every_key_and_mouse_event(self):
        self.output.down("W")
        self.focus_mock.return_value = None
        with self.assertRaises(RuntimeError):
            self.output.down("SPACE")
        with self.assertRaises(RuntimeError):
            self.output.move(10, 0)
        self.output.inhibit()
        self.assertFalse(self.output.keys)
        self.assertEqual(self.send_mock.call_count, 2)  # down W, release W

    def test_stale_frame_blocks_input(self):
        self.output.frame(self.target, time.monotonic() - 2)
        with self.assertRaises(RuntimeError):
            self.output.down("W")
        self.send_mock.assert_not_called()

    def test_inhibit_prevents_further_input(self):
        self.output.down("W")
        self.output.down("SHIFT")
        self.output.inhibit()
        self.assertFalse(self.output.keys)
        with self.assertRaises(RuntimeError):
            self.output.down("W")

    def test_pending_tap_gets_released(self):
        self.output.action({"kind": "key", "key": "SPACE"})
        self.assertIn("SPACE", self.output.keys)
        self.output.flush(time.monotonic() + 1)
        self.assertFalse(self.output.keys)
        self.assertEqual(self.output.pending, [])

    def test_failed_send_still_attempts_release(self):
        self.send_mock.side_effect = [OSError("blocked"), None]
        with self.assertRaises(OSError):
            self.output.down("W")
        self.output.inhibit()
        self.assertFalse(self.output.keys)
        self.assertEqual(self.send_mock.call_count, 2)

    def test_window_geometry_change_blocks_input(self):
        self.focus_mock.return_value = win.Target(123, (1, 0, 1921, 1080), "Fortnite", "game.exe")
        with self.assertRaises(RuntimeError):
            self.output.move(1, 0)
        self.send_mock.assert_not_called()

    def test_menu_mouse_button_released_on_emergency_stop(self):
        self.output.action({"kind": "click", "point": [.5, .5]})
        self.assertTrue(self.output.mouse_down)
        self.focus_mock.return_value = None
        self.output.inhibit()
        self.assertFalse(self.output.mouse_down)
        self.assertFalse(self.output.pending)
        self.assertEqual(self.send_mock.call_count, 3)  # move, down, up
