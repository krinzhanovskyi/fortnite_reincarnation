from copy import deepcopy
import random
import unittest

from reincarnation.behavior import Behavior
from reincarnation.capture_profile import ReplayInput
from reincarnation.config import defaults, validate


class RoamingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = defaults()["behavior"]
        self.cfg.update(weights={"move": 1, "look": 0, "pause": 0}, move_seconds=[8, 8],
                        sprint_probability=1, turn_probability=1, jump_probability=0,
                        diagonal_probability=1)
        self.output = ReplayInput()
        self.behavior = Behavior(self.cfg, self.output, random.Random(7))

    def test_outward_walk_is_capped_then_reversed_with_same_camera(self):
        b = self.behavior
        b.tick(0)
        self.assertEqual(b.end, 2.5)
        outward = set(self.output.keys)
        self.assertIn("W", outward)
        self.assertNotIn("SHIFT", outward)
        b.tick(1.25)
        self.assertFalse(any(e[0] == "move" for e in self.output.events))
        b.tick(2.5)
        self.assertEqual(b.kind, "return")
        opposites = {"W": "S", "A": "D", "D": "A"}
        self.assertEqual(self.output.keys, {opposites[k] for k in outward})
        b.tick(3.5)
        self.assertFalse(any(e[0] == "move" for e in self.output.events))
        b.tick(5)
        self.assertEqual(b.kind, "pause")
        self.assertFalse(self.output.keys)
        b.tick(b.end)
        self.assertEqual(b.kind, "look")
        self.assertFalse(self.output.keys)
        self.assertGreaterEqual(abs(b.total_turn), 120)
        b.tick((b.start + b.end) / 2)
        self.assertTrue(any(e[0] == "move" for e in self.output.events))

    def test_pause_clears_pending_return_instead_of_sending_input_later(self):
        b = self.behavior
        b.tick(0)
        self.assertTrue(b.return_keys)
        b.reset()
        self.assertFalse(self.output.keys)
        self.assertFalse(b.return_keys)
        self.assertFalse(b.need_reorient)
        b.tick(10)
        self.assertEqual(b.kind, "move")
        self.assertIn("W", self.output.keys)

    def test_legacy_behavior_can_be_selected(self):
        self.cfg["roaming"]["enabled"] = False
        b = self.behavior
        b.tick(0)
        self.assertEqual(b.end, 8)
        self.assertIn("SHIFT", self.output.keys)
        b.tick(4)
        self.assertTrue(any(e[0] == "move" for e in self.output.events))
        self.assertFalse(b.return_keys)

    def test_return_duration_is_adjustable_and_no_contradictory_keys(self):
        self.cfg["roaming"]["return_multiplier"] = 1.2
        b = self.behavior
        b.tick(0)
        b.tick(2.5)
        self.assertAlmostEqual(b.end - b.start, 3)
        for i in range(1, 2000):
            b.tick(2.5 + i * .05)
            self.assertFalse({"W", "S"}.issubset(self.output.keys))
            self.assertFalse({"A", "D"}.issubset(self.output.keys))
            self.assertNotIn("SHIFT", self.output.keys)

    def test_invalid_bounds_and_direction_bindings_are_rejected(self):
        for key, value in (("max_outward_seconds", 0), ("return_multiplier", 10),
                           ("enabled", "true"), ("reorient_pixels", [400, 20])):
            cfg = defaults()
            cfg["behavior"]["roaming"][key] = value
            with self.assertRaises(ValueError):
                validate(cfg)
        cfg = defaults()
        cfg["behavior"]["keys"]["backward"] = "W"
        with self.assertRaises(ValueError):
            validate(cfg)


if __name__ == "__main__":
    unittest.main()
