from __future__ import annotations

import math
import random


class Behavior:
    """Nonblocking episodes. Output owns the pressed-key set and focus checks."""

    def __init__(self, cfg: dict, output, rng=None, log=None):
        self.cfg, self.output = cfg, output
        self.rng = rng or random.Random()
        self.log = log or (lambda _: None)
        self.end = 0.0
        self.start = 0.0
        self.kind = "idle"
        self.total_turn = 0.0
        self.sent_turn = 0
        self.jump_at = math.inf
        self.jump_up = math.inf
        self.last_kind = None
        self.return_keys = []
        self.return_seconds = 0.0
        self.need_reorient = False

    def reset(self):
        self.output.release_all()
        self.end = 0.0
        self.jump_at = self.jump_up = math.inf
        self.last_kind = None
        self.kind = "idle"
        self.return_keys = []
        self.return_seconds = 0.0
        self.need_reorient = False

    def tick(self, now: float):
        c, r = self.cfg, self.rng
        if now >= self.end:
            self.output.release_all()
            roam = c["roaming"]
            reorient = False
            if roam["enabled"] and self.return_keys:
                self.kind = "return"
                duration = self.return_seconds
            elif roam["enabled"] and self.kind == "return":
                self.kind = "pause"
                duration = r.uniform(*roam["return_pause_seconds"])
                self.need_reorient = True
            elif roam["enabled"] and self.need_reorient:
                self.kind = "look"
                duration = r.uniform(*c["look_seconds"])
                reorient = True
                self.need_reorient = False
            else:
                names = ["move", "look", "pause"]
                weights = [c["weights"][n] * (.6 if self.last_kind == n else 1) for n in names]
                self.kind = r.choices(names, weights=weights)[0]
                duration = r.uniform(*c[f"{self.kind}_seconds"])
            if self.kind == "move" and roam["enabled"]:
                duration = min(duration, roam["max_outward_seconds"])
            self.last_kind = self.kind
            self.start, self.end = now, now + duration
            self.sent_turn = 0
            self.total_turn = 0
            self.jump_at = self.jump_up = math.inf
            if self.kind == "return":
                # With a fixed camera, opposite keys roughly retrace the walk.
                # This is a movement bound, not an estimate of world position.
                for key in self.return_keys:
                    self.output.down(key)
                self.log(f"ROAM return: {','.join(self.return_keys)}, {duration:.2f}s")
                self.return_keys = []
            elif self.kind == "move":
                self.output.down(c["keys"]["forward"])
                reverse = [c["keys"]["backward"]]
                if r.random() < c["diagonal_probability"]:
                    side = r.choice(["left", "right"])
                    self.output.down(c["keys"][side])
                    reverse.append(c["keys"]["right" if side == "left" else "left"])
                if not roam["enabled"] and r.random() < c["sprint_probability"]:
                    self.output.down(c["keys"]["sprint"])
                if r.random() < c["jump_probability"]:
                    self.jump_at = now + duration * r.uniform(.2, .7)
                if roam["enabled"]:
                    self.return_keys = reverse
                    self.return_seconds = duration * roam["return_multiplier"]
            if self.kind == "look" or (self.kind == "move" and not roam["enabled"] and r.random() < c["turn_probability"]):
                turn_range = roam["reorient_pixels"] if reorient else c["turn_pixels"]
                self.total_turn = r.uniform(*turn_range) * r.choice([-1, 1]) * c["turn_sensitivity"]
        if self.total_turn:
            fraction = min(1, max(0, (now - self.start) / (self.end - self.start)))
            target = round(self.total_turn * (fraction * fraction * (3 - 2 * fraction)))
            delta = target - self.sent_turn
            if delta:
                self.output.move(delta, 0)
                self.sent_turn = target
        if now >= self.jump_at:
            self.output.down(c["keys"]["jump"])
            self.jump_at = math.inf
            self.jump_up = now + c["jump_hold"]
        if now >= self.jump_up:
            self.output.up(c["keys"]["jump"])
            self.jump_up = math.inf
