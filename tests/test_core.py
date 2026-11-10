from copy import deepcopy
from pathlib import Path
import random
import tempfile
import unittest

from PIL import Image, ImageDraw

from reincarnation.behavior import Behavior
from reincarnation.config import defaults, load, readiness, save, validate
from reincarnation.engine import Engine
from reincarnation.vision import Observation, Recognizer


class FakeInput:
    def __init__(self):
        self.keys = set()
        self.events = []

    def down(self, key):
        self.keys.add(key)
        self.events.append(("down", key))

    def up(self, key):
        self.keys.discard(key)
        self.events.append(("up", key))

    def release_all(self):
        self.keys.clear()

    def action(self, spec):
        self.events.append(("action", deepcopy(spec)))

    def move(self, dx, dy):
        self.events.append(("move", dx, dy))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = defaults()
        self.cfg["runtime"].update(observe_only=False, stable_frames=2, entry_delay=.1, match_settle=0)
        self.cfg["profile"]["actions"]["lobby"]["point"] = [.5, .5]
        self.cfg["profile"]["actions"]["results"]["point"] = [.5, .7]
        self.output = FakeInput()
        self.logs = []
        self.engine = Engine(self.cfg, self.output, self.logs.append, random.Random(9))
        self.now = 0
        self.engine.start(self.now)

    def frame(self, state, step=.2):
        self.now += step
        self.engine.tick(Observation(state, 1), True, self.now)

    def state(self, state):
        for _ in range(3):
            self.frame(state)
        self.assertEqual(self.engine.state, state)

    def actions(self):
        return [e for e in self.output.events if e[0] == "action"]

    def test_two_full_cycles_without_app_restart(self):
        for cycle in range(2):
            for state in ("lobby", "searching", "loading", "waiting", "bus", "deploying", "match"):
                self.state(state)
            self.assertTrue(self.output.keys)
            self.frame("results")  # First frame already releases, before debounce.
            self.assertFalse(self.output.keys)
            self.state("results")
            self.assertEqual(self.engine.cycles, cycle + 1)
        self.state("lobby")
        self.assertEqual(len(self.actions()), 7)  # lobby, bus, results x2, next lobby
        self.assertTrue(self.engine.running)

    def test_focus_loss_releases_and_requires_explicit_start(self):
        self.state("match")
        self.output.down("W")
        self.engine.tick(Observation("match", 1), False, self.now + .1)
        self.assertFalse(self.output.keys)
        self.assertFalse(self.engine.running)
        self.frame("match")
        self.assertFalse(self.engine.running)

    def test_unknown_screen_stops_immediately_then_pauses(self):
        self.state("match")
        self.output.down("W")
        self.frame("unknown")
        self.assertFalse(self.output.keys)
        self.frame("unknown", 21)
        self.assertFalse(self.engine.running)

    def test_unknown_recovery_requires_debounce(self):
        self.cfg["behavior"]["weights"] = {"move": 1, "look": 0, "pause": 0}
        self.state("match")
        self.frame("unknown")
        self.frame("match")
        self.assertFalse(self.output.keys)
        self.frame("match")
        self.assertIn("W", self.output.keys)

    def test_menu_attempts_bounded_and_not_repeated_per_frame(self):
        self.state("lobby")
        for _ in range(10):
            self.frame("lobby")
        self.assertEqual(len(self.actions()), 1)
        for _ in range(3):
            self.frame("lobby", 19)
        self.assertEqual(len(self.actions()), 3)
        self.assertFalse(self.engine.running)

    def test_observation_never_sends_input(self):
        self.cfg["runtime"]["observe_only"] = True
        for state in ("lobby", "searching", "loading", "bus", "deploying", "match", "results"):
            self.state(state)
        self.assertEqual(self.output.events, [])

    def test_single_false_result_frame_releases_without_transition(self):
        self.state("match")
        self.output.down("W")
        self.frame("results")
        self.assertEqual(self.engine.state, "match")
        self.assertFalse(self.output.keys)

    def test_hud_on_waiting_island_does_not_start_match(self):
        self.state("loading")
        self.frame("match")
        self.assertEqual(self.engine.state, "loading")
        self.assertFalse(self.output.keys)

    def test_state_timeout(self):
        self.state("searching")
        self.frame("searching", 601)
        self.assertFalse(self.engine.running)

    def test_missing_intermediate_frames(self):
        for state in ("lobby", "loading", "bus", "match", "results", "lobby"):
            self.state(state)
        self.assertEqual(self.engine.cycles, 1)

    def test_menu_variant_requires_confirmation_and_has_shared_retry_budget(self):
        self.state("results")
        self.assertEqual(len(self.actions()), 1)
        new = Observation("results", 1, "confirmation", "confirm.png", {"kind": "key", "key": "ENTER"})
        self.now += .2
        self.engine.tick(new, True, self.now)
        self.assertEqual(len(self.actions()), 1)
        self.now += .2
        self.engine.tick(new, True, self.now)
        self.assertEqual(len(self.actions()), 2)
        self.assertEqual(self.actions()[-1][1]["key"], "ENTER")
        self.assertEqual(self.engine.attempts, 2)

    def test_visual_variants_with_same_action_do_not_repeat_menu_click(self):
        self.state("lobby")
        for name in ("idle.png", "pulse.png", "idle.png"):
            for _ in range(2):
                self.now += .2
                self.engine.tick(Observation("lobby", 1, "template", name), True, self.now)
        self.assertEqual(len(self.actions()), 1)

    def test_synthetic_screen_to_screen_full_cycle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            specs, frames = [], {}
            states = ("lobby", "searching", "loading", "waiting", "bus", "deploying", "match", "results")
            for i, state in enumerate(states):
                frame = Image.new("RGB", (300, 200), (20 + i * 25, 40, 60))
                draw = ImageDraw.Draw(frame)
                # Eight distinct binary patterns in a fixed UI region.
                for bit in range(3):
                    draw.rectangle((10 + bit * 30, 10, 39 + bit * 30, 49), fill="white" if i & (1 << bit) else "black")
                box = (10, 10, 100, 50)
                frame.crop(box).save(root / f"{state}.png")
                specs.append({"state": state, "box": box, "file": f"{state}.png", "threshold": .98})
                frames[state] = frame
            recognizer = Recognizer({"size": [300, 200], "templates": specs}, root)
            for state in (*states, "lobby"):
                for _ in range(3):
                    self.now += .2
                    self.engine.tick(recognizer.classify(frames[state]), True, self.now)
                self.assertEqual(self.engine.state, state)
            self.assertEqual(self.engine.cycles, 1)
            self.assertEqual(len(self.actions()), 4)


class VisionAndConfigTests(unittest.TestCase):
    def test_defaults_roundtrip_and_incomplete_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            cfg = load(path)
            self.assertTrue(cfg["runtime"]["observe_only"])
            self.assertTrue(readiness(cfg, path.parent))
            save(path, cfg)
            self.assertEqual(load(path), cfg)

    def test_invalid_settings_rejected(self):
        for section, key, value in (("runtime", "max_attempts", 0), ("runtime", "max_attempts", 1.2),
                                     ("behavior", "jump_probability", 1.5),
                                     ("behavior", "turn_sensitivity", float("nan")),
                                     ("runtime", "observe_only", "false")):
            with self.subTest(key=key):
                cfg = defaults()
                cfg[section][key] = value
                with self.assertRaises(ValueError):
                    validate(cfg)

    def test_real_image_matching_and_results_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            frame = Image.new("RGB", (300, 200), "#123456")
            draw = ImageDraw.Draw(frame)
            draw.rectangle((20, 20, 80, 40), fill="white")
            draw.text((22, 22), "HUD", fill="black")
            draw.rectangle((100, 100, 200, 140), fill="yellow")
            specs = []
            for state, box in (("match", (20, 20, 80, 40)), ("results", (100, 100, 200, 140))):
                filename = f"{state}.png"
                frame.crop(box).save(root / filename)
                specs.append({"state": state, "box": box, "file": filename, "threshold": .98})
            recognizer = Recognizer({"size": [300, 200], "templates": specs}, root)
            self.assertEqual(recognizer.classify(frame).state, "results")
            draw.rectangle((100, 100, 200, 140), fill="black")
            self.assertEqual(recognizer.classify(frame).state, "match")
            self.assertEqual(recognizer.classify(Image.new("RGB", (200, 200))).state, "unknown")
            self.assertEqual(recognizer.classify(Image.new("RGB", (300, 200))).state, "unknown")

    def test_behavior_varies_and_keys_are_released(self):
        output = FakeInput()
        behavior = Behavior(defaults()["behavior"], output, random.Random(42))
        kinds = set()
        for i in range(4000):
            behavior.tick(i * .05)
            kinds.add(behavior.kind)
            self.assertFalse({"A", "D"}.issubset(output.keys))
        self.assertEqual(kinds, {"move", "return", "look", "pause"})
        behavior.reset()
        self.assertFalse(output.keys)
        self.assertTrue(any(e[0] == "move" for e in output.events))


if __name__ == "__main__":
    unittest.main()
