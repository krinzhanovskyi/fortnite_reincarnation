from __future__ import annotations

from .behavior import Behavior
from .vision import Observation
from .config import STATES

# Missing intermediate screen frames are normal. Regressions are not accepted.
TRANSITIONS = {
    "unknown": set(STATES),
    "lobby": {"searching", "loading", "waiting", "bus_wait", "bus"},
    "searching": {"lobby", "loading", "waiting", "bus_wait", "bus"},
    "loading": {"lobby", "waiting", "bus_wait", "bus", "deploying", "results"},
    "waiting": {"bus_wait", "bus", "deploying", "results"},
    "bus_wait": {"bus", "deploying", "results"},
    "bus": {"deploying", "match", "results"},
    "deploying": {"match", "downed", "eliminated", "spectating", "continue", "xp", "results"},
    "match": {"downed", "eliminated", "spectating", "continue", "xp", "results", "lobby", "loading"},
    "downed": {"match", "eliminated", "spectating", "continue", "xp", "results", "loading", "lobby"},
    "eliminated": {"spectating", "continue", "xp", "results", "deploying", "match", "lobby", "loading"},
    "spectating": {"eliminated", "continue", "xp", "results", "deploying", "match", "lobby", "loading", "searching"},
    "continue": {"xp", "results", "spectating", "searching", "loading", "lobby"},
    "xp": {"results", "spectating", "searching", "loading", "lobby"},
    "results": {"lobby", "searching", "loading", "waiting", "bus_wait", "bus"},
}


class Engine:
    def __init__(self, cfg: dict, output, log, rng=None):
        self.cfg, self.output, self.log = cfg, output, log
        self.behavior = Behavior(cfg["behavior"], output, rng, log)
        self.running = False
        self.state = "unknown"
        self.reason = "Paused"
        self.candidate = "unknown"
        self.count = 0
        self.candidate_template = None
        self.candidate_action = None
        self.screen_template = None
        self.screen_action = None
        self.entered = 0.0
        self.unknown_since = None
        self.last_action = None
        self.attempts = 0
        self.cycles = 0
        self.was_in_match = False

    @property
    def observing(self):
        return self.cfg["runtime"]["observe_only"]

    def start(self, now: float):
        self.behavior.reset()
        self.running = True
        self.state = "unknown"
        self.candidate, self.count = "unknown", 0
        self.candidate_template = self.screen_template = None
        self.candidate_action = None
        self.screen_action = None
        self.entered = now
        self.unknown_since = None
        self.last_action, self.attempts = None, 0
        self.was_in_match = False
        self.reason = "Observing" if self.observing else "Running"
        self.log(self.reason)

    def pause(self, reason="Manual pause"):
        self.running = False
        self.reason = reason
        self.behavior.reset()
        self.log(f"PAUSE: {reason}")

    def tick(self, obs: Observation, focused: bool, now: float):
        if not self.running:
            return
        if not focused:
            self.pause("Game lost focus")
            return
        r = self.cfg["runtime"]
        allowed = obs.state == self.state or obs.state in TRANSITIONS[self.state]
        if obs.state == "unknown" or not allowed:
            # Stop on the first suspect frame, before debounce and unknown timeout.
            self.behavior.reset()
            self.candidate, self.count = "unknown", 0
            self.candidate_template = None
            self.candidate_action = None
            if self.unknown_since is None:
                self.unknown_since = now
            if now - self.unknown_since >= r["unknown_timeout"]:
                self.pause(f"Unrecognized/unexpected screen: {obs.state}; {obs.detail}")
            return
        self.unknown_since = None
        if obs.state != self.candidate or obs.action != self.candidate_action:
            self.candidate, self.count = obs.state, 1
            self.candidate_template = obs.template
            self.candidate_action = obs.action
        else:
            self.count += 1
        if self.count < r["stable_frames"]:
            self.behavior.reset()
            return
        if obs.state != self.state:
            self.behavior.reset()
            old = self.state
            self.state, self.entered = obs.state, now
            self.last_action, self.attempts = None, 0
            if self.state == "match":
                self.was_in_match = True
            if self.was_in_match and self.state in ("continue", "xp", "results", "lobby"):
                self.cycles += 1
                self.was_in_match = False
            self.log(f"STATE {old} -> {self.state} ({obs.score:.3f})")
        # Different visual variants of the same button must not trigger retries.
        # A confirmed variant needing a different action can advance immediately.
        # The total retry budget still bounds alternating confirmation dialogs.
        effective_action = obs.action or self.cfg["profile"]["actions"].get(self.state)
        if effective_action != self.screen_action:
            self.last_action = None
        self.screen_template, self.screen_action = obs.template, effective_action
        if now - self.entered > r["state_timeouts"][self.state]:
            self.pause(f"State timeout: {self.state}")
            return
        if self.observing:
            return
        if effective_action and effective_action["kind"] == "wait":
            self.behavior.reset()
            return
        if self.state == "match":
            if now - self.entered >= r["match_settle"]:
                self.behavior.tick(now)
            return
        action = obs.action or self.cfg["profile"]["actions"].get(self.state)
        if action is None or now - self.entered < max(r["entry_delay"], action.get("delay", 0)):
            return
        if action["kind"] == "play":
            raise ValueError("Play action outside match")
        # No new action while waiting for the visible result of the last one.
        if self.last_action is not None and now - self.last_action < r["action_timeout"]:
            return
        if self.attempts >= r["max_attempts"]:
            self.pause(f"No confirmed transition after {self.attempts} attempts: {self.state}")
            return
        self.output.action(action)
        self.attempts += 1
        self.last_action = now
        self.log(f"ACTION {self.state}: {action['kind']}, attempt {self.attempts}/{r['max_attempts']}")
