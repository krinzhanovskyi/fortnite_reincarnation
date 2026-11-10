from copy import deepcopy
from pathlib import Path
import random
import tempfile
import unittest

from PIL import Image, ImageDraw

from reincarnation.capture_profile import CAPTURES, ReplayInput, audit
from reincarnation.config import defaults, load
from reincarnation.engine import Engine
from reincarnation.vision import Observation, Recognizer, Region


class DetailedStateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = defaults()
        self.cfg["runtime"].update(observe_only=False, stable_frames=2, entry_delay=.1, match_settle=0)
        self.output = ReplayInput()
        self.engine = Engine(self.cfg, self.output, lambda _: None, random.Random(7))
        self.engine.start(0)
        self.now = 0

    def feed(self, state, action=None, frames=3, step=.2):
        for _ in range(frames):
            self.now += step
            self.engine.tick(Observation(state, 1, action=action), True, self.now)
        self.assertEqual(self.engine.state, state)

    def test_downed_releases_on_first_frame_and_revive_resumes(self):
        self.cfg["behavior"]["weights"] = {"move": 1, "look": 0, "pause": 0}
        self.feed("match")
        self.assertIn("W", self.output.keys)
        self.now += .2
        self.engine.tick(Observation("downed", 1, action={"kind": "wait"}), True, self.now)
        self.assertFalse(self.output.keys)
        self.assertEqual(self.engine.state, "match")  # Transition still needs confirmation.
        self.feed("downed", {"kind": "wait"})
        self.feed("match")
        self.assertIn("W", self.output.keys)

    def test_death_menu_sequence_and_reboot_do_not_double_count(self):
        self.feed("match")
        self.feed("downed")
        self.assertFalse(self.output.keys)
        self.feed("match")  # revive
        self.feed("eliminated")
        self.assertFalse(self.output.keys)
        self.assertEqual(self.engine.cycles, 0)
        self.feed("spectating")
        self.feed("deploying")  # reboot by teammate
        self.feed("match")
        self.feed("eliminated")
        self.feed("continue", {"kind": "wait"})
        self.feed("xp", {"kind": "wait"})
        self.feed("results", {"kind": "wait"})
        self.assertEqual(self.engine.cycles, 1)
        self.feed("searching")
        self.assertTrue(self.engine.running)

    def test_no_bus_action_before_available_prompt(self):
        self.feed("waiting")
        self.feed("bus_wait", frames=20)
        self.assertFalse(self.output.events)
        self.feed("bus")
        self.assertEqual([e[1] for e in self.output.events if e[0] == "action"], [{"kind": "key", "key": "SPACE"}])

    def test_menu_delay_and_wait_override(self):
        action = {"kind": "click", "point": [.9, .95], "delay": 6}
        self.feed("continue", action)
        self.feed("continue", action, frames=10)
        self.assertFalse(self.output.events)
        self.feed("continue", action, frames=25)
        self.assertEqual(len([e for e in self.output.events if e[0] == "action"]), 1)

    def test_text_mask_matches_on_changed_background_but_not_other_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = Image.new("RGB", (200, 50), "#254b6a")
            ImageDraw.Draw(source).text((8, 8), "WAIT FOR PLAYERS", fill="white")
            source.save(root / "text.png")
            region = Region({"box": [0, 0, 200, 50], "file": "text.png", "metric": "white_text", "threshold": .94}, root)
            changed = Image.new("RGB", source.size, "#632572")
            ImageDraw.Draw(changed).text((9, 8), "WAIT FOR PLAYERS", fill="white")
            self.assertGreaterEqual(region.score(changed), .94)
            ImageDraw.Draw(changed).rectangle((0, 0, 200, 50), fill="#632572")
            ImageDraw.Draw(changed).text((9, 8), "MATCH COMPLETE", fill="white")
            self.assertLess(region.score(changed), .94)

    def test_multiple_regions_require_local_player_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            frame = Image.new("RGB", (200, 100), "black")
            draw = ImageDraw.Draw(frame)
            draw.rectangle((5, 5, 40, 25), fill="white")
            draw.rectangle((100, 50, 180, 80), fill="yellow")
            frame.crop((5, 5, 40, 25)).save(root / "health.png")
            frame.crop((100, 50, 180, 80)).save(root / "own.png")
            spec = {"state": "match", "box": [5, 5, 40, 25], "file": "health.png", "threshold": .98,
                    "checks": [{"box": [100, 50, 180, 80], "file": "own.png", "threshold": .98}]}
            recognizer = Recognizer({"size": [200, 100], "templates": [spec]}, root)
            self.assertEqual(recognizer.classify(frame).state, "match")
            draw.rectangle((100, 50, 180, 80), fill="black")
            self.assertEqual(recognizer.classify(frame).state, "unknown")


ROOT = Path(__file__).resolve().parents[1] / "runtime"
HAS_REVIEWED = (ROOT / "calibration" / "settings.calibrated.json").is_file() and all(
    (ROOT / c.source_folder / f"{c.stem}.png").is_file() for c in CAPTURES)


@unittest.skipUnless(HAS_REVIEWED, "Reviewed private capture files are not distributed with source")
class ReviewedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load(ROOT / "calibration" / "settings.calibrated.json")

    def test_every_reviewed_capture_state_and_executed_action(self):
        report = audit(ROOT, self.cfg)
        self.assertEqual(report["passed"], report["reviewed"], report["entries"])
        self.assertFalse(report["readiness"])

    def test_complete_real_capture_sequence_with_fake_input(self):
        cfg = deepcopy(self.cfg)
        cfg["runtime"]["observe_only"] = False
        recognizer = Recognizer(cfg["profile"], ROOT)
        output = ReplayInput()
        engine = Engine(cfg, output, lambda _: None, random.Random(7))
        engine.start(0)
        # Includes both descent prompts and a downed state, then the full menus.
        indices = (0, 2, 8, 3, 5, 10, 11, 13, 14, 7, 15, 16, 17, 18, 0, 2, 20, 21, 22)
        now = 0
        for index in indices:
            capture = CAPTURES[index]
            with Image.open(ROOT / "captures" / f"{capture.stem}.png") as image:
                obs = recognizer.classify(image)
            for _ in range(24):
                now += .5
                engine.tick(obs, True, now)
            self.assertTrue(engine.running, (capture, engine.reason))
            self.assertEqual(engine.state, capture.state)
            if capture.state != "match":
                self.assertFalse(output.keys)
        self.assertEqual(engine.cycles, 1)
        menu = [e[1] for e in output.events if e[0] == "action"]
        self.assertEqual(menu, [cfg["profile"]["actions"][state] for state in
                                ("lobby", "bus", "continue", "xp", "results", "lobby")])

    def test_unrelated_blank_frames_never_start_movement_or_menu(self):
        recognizer = Recognizer(self.cfg["profile"], ROOT)
        for color in ("black", "white", "#a0a0a0", "#25b725"):
            self.assertEqual(recognizer.classify(Image.new("RGB", (1920, 1080), color)).state, "unknown")

    def test_center_spectator_header_generalizes_between_both_real_frames(self):
        # Do not learn the second scene/timer value: recognize it from the first.
        cfg = deepcopy(self.cfg)
        cfg["profile"]["templates"] = [t for t in cfg["profile"]["templates"]
                                          if "1794326040030763500" not in t["file"]]
        recognizer = Recognizer(cfg["profile"], ROOT)
        cfg["runtime"]["observe_only"] = False
        output = ReplayInput()
        engine = Engine(cfg, output, lambda _: None, random.Random(7))
        engine.start(0)
        for now in (1, 2, 3):
            engine.tick(Observation("eliminated", 1, action={"kind": "wait"}), True, now)
        for stem in ("1794325960967508000", "1794326040030763500"):
            capture = next(c for c in CAPTURES if c.stem == stem)
            with Image.open(ROOT / capture.source_folder / f"{stem}.png") as image:
                obs = recognizer.classify(image)
            self.assertEqual(obs.state, "spectating")
            self.assertEqual(obs.action, {"kind": "wait"})
            for _ in range(130):
                now += 1
                engine.tick(obs, True, now)
                self.assertTrue(engine.running, engine.reason)
                self.assertFalse(output.keys)
        self.assertFalse(output.events)
        for _ in range(3):
            now += 1
            engine.tick(Observation("xp", 1, action={"kind": "wait"}), True, now)
        self.assertEqual(engine.state, "xp")

    def test_reboot_after_spectating_can_resume_local_character(self):
        cfg = deepcopy(self.cfg)
        cfg["runtime"].update(observe_only=False, match_settle=0)
        cfg["behavior"]["weights"] = {"move": 1, "look": 0, "pause": 0}
        recognizer = Recognizer(cfg["profile"], ROOT)
        output = ReplayInput()
        engine = Engine(cfg, output, lambda _: None, random.Random(7))
        engine.start(0)
        now = 0
        for stem in ("1794326040030763500", "1794316736306605900"):
            with Image.open(ROOT / "captures" / f"{stem}.png") as image:
                obs = recognizer.classify(image)
            for _ in range(4):
                now += .5
                engine.tick(obs, True, now)
        self.assertEqual(engine.state, "match")
        self.assertIn("W", output.keys)


if __name__ == "__main__":
    unittest.main()
