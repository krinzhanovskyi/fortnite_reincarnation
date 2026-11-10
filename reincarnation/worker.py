from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import threading
import time

from .config import load, readiness
from .engine import Engine
from .vision import Observation, Recognizer
from .recovery import Recovery
from .windows import Input, capture, foreground


class Worker:
    def __init__(self, root: Path, events: queue.Queue):
        self.root, self.events = root, events
        self.config_path = root / "settings.json"
        self.cfg = load(self.config_path)
        self.commands = queue.Queue()
        self.shutdown = threading.Event()
        self.interrupt = threading.Event()
        self.manual_stop = threading.Event()
        self.manual_stop.set()
        self.control_lock = threading.RLock()
        self.control_generation = 0
        self.recovery = Recovery(self.cfg["recovery"])
        self.logger = logging.getLogger(f"reincarnation.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        (root / "logs").mkdir(parents=True, exist_ok=True)
        self.handler = RotatingFileHandler(root / "logs" / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(self.handler)
        self.output = Input(self.cfg["window"], self.cfg["runtime"]["max_frame_age"])
        self.engine = Engine(self.cfg, self.output, self.log)
        self.recognizer = Recognizer(self.cfg["profile"], root)
        self.last_frame = None
        self.last_obs = Observation()
        self.thread = threading.Thread(target=self._run, name="controller", daemon=True)
        self.watchdog = threading.Thread(target=self._watch, name="input-watchdog", daemon=True)
        self.watchdog.start()
        self.thread.start()

    def log(self, message):
        self.logger.info(message)
        self.events.put(("log", message))

    def _watch(self):
        # Independent from capture/recognition: release even if they stall.
        while not self.shutdown.wait(.02):
            try:
                with self.output.lock:
                    if not self.output.enabled:
                        continue
                    current = foreground(self.output.spec)
                    if (current is None or
                        (self.output.target is not None and current != self.output.target) or
                        time.monotonic() - self.output.frame_at > self.output.max_frame_age):
                        self.interrupt.set()
                        self.output.inhibit()
                        self.log("WATCHDOG: input released (focus/geometry/freshness)")
            except Exception as exc:
                self.interrupt.set()
                try:
                    self.output.inhibit()
                except Exception as release_error:
                    self.log(f"WATCHDOG RELEASE ERROR: {release_error}")
                self.log(f"WATCHDOG ERROR: {exc}")

    def request(self, action):
        with self.control_lock:
            if action in ("start", "pause", "stop", "capture", "recover"):
                self.control_generation += 1
            if action in ("pause", "stop", "capture"):
                # Cancel recovery synchronously, including an in-flight reload.
                self.manual_stop.set()
                self.interrupt.set()
                try:
                    self.output.inhibit()
                except Exception as exc:
                    self.log(f"RELEASE ERROR: {exc}")
            elif action == "recover":
                self.interrupt.set()
                try:
                    self.output.inhibit()
                except Exception as exc:
                    self.log(f"RELEASE ERROR: {exc}")
            self.commands.put((action, self.control_generation))

    def _reload_start(self, generation):
        self.output.inhibit()
        cfg = load(self.config_path)
        problems = readiness(cfg, self.root)
        if problems and not cfg["runtime"]["observe_only"]:
            raise ValueError("Calibration incomplete: " + "; ".join(problems))
        recognizer = Recognizer(cfg["profile"], self.root)
        if not foreground(cfg["window"]):
            raise RuntimeError("Bring the configured game window to foreground, then press Start hotkey")
        with self.control_lock:
            if generation != self.control_generation or self.manual_stop.is_set() or self.shutdown.is_set():
                return False
            cycles = self.engine.cycles
            self.recognizer = recognizer
            self.cfg = cfg
            self.recovery.cfg = cfg["recovery"]
            self.output.spec = cfg["window"]
            self.output.max_frame_age = cfg["runtime"]["max_frame_age"]
            self.engine = Engine(cfg, self.output, self.log)
            self.engine.cycles = cycles
            self.last_obs = Observation()
            self.interrupt.clear()
            self.output.arm()
            self.engine.start(time.monotonic())
            return True

    def _fault(self, reason, diagnostic=True):
        self.interrupt.set()
        self.engine.running = False
        self.engine.reason = reason
        try:
            self.output.inhibit()
            self.engine.pause(reason)
        except Exception as exc:
            self.log(f"RELEASE ERROR: {exc}")
        with self.control_lock:
            if self.manual_stop.is_set() or self.shutdown.is_set():
                self.recovery.cancel()
                return
            pending = self.recovery.failure()
        if pending:
            self.log(f"AUTO RESTART scheduled: {reason}")
        elif self.recovery.wanted and self.recovery.cfg["enabled"]:
            self.engine.reason = f"Restart limit reached ({self.recovery.attempts}); press Start to retry"
            self.log(self.engine.reason)
        if diagnostic:
            try:
                self._diagnostic()
            except Exception as exc:
                self.log(f"Diagnostic save failed: {exc}")

    def _try_recover(self):
        with self.control_lock:
            if self.manual_stop.is_set() or self.shutdown.is_set():
                self.recovery.cancel()
                return False
            if not self.recovery.pending:
                return False
            generation = self.control_generation
        # Losing focus never sends input to another window or consumes retries.
        focus_error = None
        try:
            focused = foreground(self.cfg["window"])
        except Exception as exc:
            focused, focus_error = True, exc
        if not focused:
            self.engine.reason = "Auto restart: waiting for game focus"
            return False
        with self.control_lock:
            if generation != self.control_generation or self.manual_stop.is_set():
                return False
            if not self.recovery.reserve():
                return False
            attempt = self.recovery.attempts
        self.log(f"AUTO RESTART {attempt}/{self.recovery.cfg['max_restarts']}")
        try:
            if focus_error is not None:
                raise RuntimeError(f"Focus check failed: {focus_error}") from focus_error
            return self._reload_start(generation)
        except Exception as exc:
            self.log(f"AUTO RESTART failed: {exc}")
            self._fault(f"Restart error: {exc}", diagnostic=False)
            return False

    def _snapshot(self):
        if self.engine.running:
            self.request("pause")
            raise RuntimeError("Pause before calibration capture")
        cfg = load(self.config_path)
        target = foreground(cfg["window"])
        if target is None:
            raise RuntimeError("Capture requires the game in foreground")
        frame = capture(target)
        if foreground(cfg["window"]) != target:
            raise RuntimeError("Focus changed during capture; try again")
        folder = self.root / "captures"
        folder.mkdir(exist_ok=True)
        path = folder / f"{time.time_ns()}.png"
        frame.save(path)
        self.log(f"Capture saved: {path.name}")
        self.events.put(("capture", (frame, path)))

    def _diagnostic(self):
        if self.last_frame is not None:
            folder = self.root / "diagnostics"
            folder.mkdir(exist_ok=True)
            path = folder / f"{time.time_ns()}.png"
            self.last_frame.save(path)
            self.log(f"Diagnostic frame: {path.name}")

    def _run(self):
        next_frame = 0.0
        next_status = 0.0
        try:
            while not self.shutdown.wait(.02):
                try:
                    while True:
                        try:
                            command, generation = self.commands.get_nowait()
                        except queue.Empty:
                            break
                        with self.control_lock:
                            if generation != self.control_generation:
                                continue
                            if command == "start":
                                self.manual_stop.clear()
                                self.recovery.activate()
                        if command == "start":
                            self._reload_start(generation)
                            next_frame = 0.0
                        elif command in ("pause", "stop"):
                            self.recovery.cancel()
                            self.engine.pause("Emergency stop" if command == "stop" else "Manual pause")
                        elif command == "capture":
                            self.recovery.cancel()
                            self.engine.pause("Calibration capture")
                            self._snapshot()
                        elif command == "recover":
                            self._fault("Interface error", diagnostic=False)
                    now = time.monotonic()
                    self.output.flush(now)
                    if not self.engine.running and self._try_recover():
                        next_frame = 0.0
                    was_running = self.engine.running
                    if self.engine.running:
                        if self.interrupt.is_set():
                            self.engine.pause("Interrupted")
                        else:
                            target = foreground(self.cfg["window"])
                            if target is None:
                                self.engine.tick(Observation(), False, now)
                            elif now >= next_frame:
                                # Freshness is measured from capture start, not completion.
                                frame_at = time.monotonic()
                                frame = capture(target)
                                observation = self.recognizer.classify(frame)
                                self.last_frame, self.last_obs = frame, observation
                                now = time.monotonic()
                                focused = foreground(self.cfg["window"]) == target
                                self.output.frame(target, frame_at)
                                if now - frame_at > self.cfg["runtime"]["max_frame_age"]:
                                    raise RuntimeError("Screen capture/recognition is too slow")
                                self.engine.tick(observation, focused, now)
                                next_frame = now + self.cfg["runtime"]["frame_interval"]
                            elif (not self.engine.observing and self.engine.state == "match"
                                  and self.last_obs.state == "match"
                                  and self.engine.candidate == "match"
                                  and self.engine.count >= self.cfg["runtime"]["stable_frames"]
                                  and (self.last_obs.action is None or self.last_obs.action["kind"] == "play")
                                  and now - self.engine.entered >= self.cfg["runtime"]["match_settle"]):
                                self.engine.behavior.tick(now)
                    if was_running and not self.engine.running:
                        self._fault(self.engine.reason)
                        # Retry immediately after release, without a sleep/backoff.
                        if self._try_recover():
                            next_frame = 0.0
                    self.recovery.healthy(now, self.engine.running and
                        self.engine.state == self.last_obs.state and self.engine.state != "unknown" and
                        self.engine.count >= self.cfg["runtime"]["stable_frames"] and
                        self.engine.unknown_since is None and not self.interrupt.is_set())
                    if now >= next_status:
                        self.events.put(("status", {"running": self.engine.running,
                            "state": self.engine.state, "reason": self.engine.reason,
                            "observing": self.engine.observing, "cycles": self.engine.cycles,
                            "action": self.engine.behavior.kind, "score": self.last_obs.score,
                            "detail": self.last_obs.detail, "observed": self.last_obs.state,
                            "restarts": self.recovery.attempts, "recovering": self.recovery.pending}))
                        next_status = now + .25
                except Exception as exc:
                    self.logger.exception("Controller error")
                    self._fault(f"Error: {exc}")
                    self._try_recover()
                    next_frame = 0.0
                    self.events.put(("error", str(exc)))
        finally:
            try:
                self.output.inhibit()
            except Exception as exc:
                self.log(f"Final release failed: {exc}")

    def close(self):
        self.request("stop")
        self.shutdown.set()
        self.thread.join(timeout=3)
        self.watchdog.join(timeout=2)
        self.handler.close()
        self.logger.removeHandler(self.handler)
