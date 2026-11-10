from __future__ import annotations

import json
import math
from pathlib import Path

STATES = ("lobby", "searching", "loading", "waiting", "bus_wait", "bus", "deploying",
          "match", "downed", "eliminated", "spectating", "continue", "xp", "results")
REQUIRED = STATES
SCANCODES = {"W": 0x11, "A": 0x1E, "S": 0x1F, "D": 0x20,
             "SPACE": 0x39, "SHIFT": 0x2A, "CTRL": 0x1D,
             "E": 0x12, "F": 0x21, "ENTER": 0x1C, "ESC": 0x01}


def defaults() -> dict:
    return {
        "window": {"process_names": ["FortniteClient-Win64-Shipping.exe"],
                   "title_contains": "Fortnite"},
        "hotkeys": {"start": "F6", "pause": "F7", "stop": "F8", "capture": "F9"},
        "recovery": {"enabled": True, "max_restarts": 3, "stable_seconds": 120},
        "runtime": {"observe_only": True, "frame_interval": 0.35, "stable_frames": 3,
                    "unknown_timeout": 20.0, "action_timeout": 18.0, "max_attempts": 3,
                    "entry_delay": 1.5, "match_settle": 4.0, "max_frame_age": 1.5,
                    "state_timeouts": {"lobby": 90, "searching": 600, "loading": 240,
                                       "waiting": 240, "bus": 100, "deploying": 180,
                                       "match": 2400, "results": 180, "bus_wait": 120,
                                       "downed": 240, "eliminated": 2400, "spectating": 2400,
                                       "continue": 120, "xp": 120}},
        "behavior": {"weights": {"move": 75, "look": 15, "pause": 10},
                     "roaming": {"enabled": True, "max_outward_seconds": 2.5,
                                 "return_multiplier": 1.0, "return_pause_seconds": [0.4, 0.9],
                                 "reorient_pixels": [120, 360]},
                     "move_seconds": [3.0, 8.0], "look_seconds": [1.0, 2.5],
                     "pause_seconds": [0.5, 2.0], "turn_pixels": [25, 160],
                     "turn_probability": 0.65, "jump_probability": 0.15,
                     "sprint_probability": 0.35, "diagonal_probability": 0.3,
                     "turn_sensitivity": 1.0, "jump_hold": 0.12,
                     "keys": {"forward": "W", "backward": "S", "left": "A", "right": "D",
                              "jump": "SPACE", "sprint": "SHIFT"}},
        "profile": {"size": None, "templates": [],
                    "actions": {"lobby": {"kind": "click", "point": None},
                                "bus": {"kind": "key", "key": "SPACE"},
                                "results": {"kind": "click", "point": None},
                                "continue": {"kind": "click", "point": None, "delay": 6},
                                "xp": {"kind": "click", "point": None},
                                "match": {"kind": "play"},
                                **{s: {"kind": "wait"} for s in ("searching", "loading", "waiting", "bus_wait",
                                                                   "deploying", "downed", "eliminated", "spectating")}}},
    }


def validate(cfg: dict) -> None:
    def number(value, lo, hi, label):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{label}: expected a number in [{lo}, {hi}]")

    r, b, p = cfg["runtime"], cfg["behavior"], cfg["profile"]
    recovery = cfg["recovery"]
    if not isinstance(recovery["enabled"], bool):
        raise ValueError("recovery.enabled must be true or false")
    if isinstance(recovery["max_restarts"], bool) or not isinstance(recovery["max_restarts"], int):
        raise ValueError("recovery.max_restarts must be an integer")
    number(recovery["max_restarts"], 1, 10, "recovery.max_restarts")
    number(recovery["stable_seconds"], 10, 3600, "recovery.stable_seconds")
    for key, lo, hi in (("frame_interval", .1, 2), ("stable_frames", 1, 15),
                        ("unknown_timeout", 2, 120), ("action_timeout", 2, 120),
                        ("max_attempts", 1, 10), ("entry_delay", .1, 30),
                        ("match_settle", 0, 30), ("max_frame_age", .2, 5)):
        number(r[key], lo, hi, key)
    for key in ("stable_frames", "max_attempts"):
        if not isinstance(r[key], int):
            raise ValueError(f"{key} must be an integer")
    if not isinstance(r["observe_only"], bool):
        raise ValueError("observe_only must be true or false")
    if r["max_frame_age"] < r["frame_interval"]:
        raise ValueError("max_frame_age must be >= frame_interval")
    for state in STATES:
        number(r["state_timeouts"][state], 5, 7200, f"timeout {state}")
    for key in ("move_seconds", "look_seconds", "pause_seconds", "turn_pixels"):
        pair = b[key]
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"{key}: expected [min, max]")
        for n in pair:
            number(n, .1 if key != "turn_pixels" else 0, 20 if key != "turn_pixels" else 1500, key)
        if pair[0] > pair[1]:
            raise ValueError(f"{key}: min > max")
    for key in ("turn_probability", "jump_probability", "sprint_probability", "diagonal_probability"):
        number(b[key], 0, 1, key)
    number(b["turn_sensitivity"], .05, 5, "turn_sensitivity")
    number(b["jump_hold"], .03, .5, "jump_hold")
    roam = b["roaming"]
    if not isinstance(roam["enabled"], bool):
        raise ValueError("roaming.enabled must be true or false")
    number(roam["max_outward_seconds"], .5, 5, "roaming.max_outward_seconds")
    number(roam["return_multiplier"], .5, 2, "roaming.return_multiplier")
    for key, lo, hi in (("return_pause_seconds", .1, 5), ("reorient_pixels", 0, 1500)):
        pair = roam[key]
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"roaming.{key}: expected [min, max]")
        for value in pair:
            number(value, lo, hi, f"roaming.{key}")
        if pair[0] > pair[1]:
            raise ValueError(f"roaming.{key}: min > max")
    for key in ("move", "look", "pause"):
        number(b["weights"][key], 0, 1000, key)
    if sum(b["weights"].values()) <= 0:
        raise ValueError("At least one behavior weight must be positive")
    for key in ("forward", "backward", "left", "right", "jump", "sprint"):
        if b["keys"][key] not in SCANCODES:
            raise ValueError(f"Unsupported key: {b['keys'][key]}")
    if len({b["keys"][key] for key in ("forward", "backward", "left", "right")}) != 4:
        raise ValueError("Directional keys must be distinct")
    hotkeys = list(cfg["hotkeys"].values())
    if len(hotkeys) != 4 or len(set(hotkeys)) != 4 or any(k not in [f"F{i}" for i in range(1, 12)] for k in hotkeys):
        raise ValueError("Four distinct hotkeys F1..F11 required")
    w = cfg["window"]
    if not w["process_names"] or not all(isinstance(n, str) and n.lower().endswith(".exe") for n in w["process_names"]):
        raise ValueError("process_names must contain executable names")
    if not isinstance(w["title_contains"], str) or not w["title_contains"]:
        raise ValueError("title_contains must not be empty")
    size = p["size"]
    if size is not None:
        if len(size) != 2 or any(not isinstance(n, int) or n < 100 or n > 16384 for n in size):
            raise ValueError("Invalid profile size")
    def validate_action(a):
        if a["kind"] == "key":
            if a["key"] not in SCANCODES:
                raise ValueError("Unsupported menu key")
        elif a["kind"] == "click":
            point = a.get("point")
            if point is not None and (len(point) != 2 or any(isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) or not 0 <= n <= 1 for n in point)):
                raise ValueError("Click point must contain two normalized coordinates")
        elif a["kind"] not in ("wait", "play"):
            raise ValueError("Action kind must be click, key, wait or play")
        if "delay" in a:
            number(a["delay"], 0, 60, "action delay")

    def validate_region(t):
        if size is None:
            raise ValueError("Templates require a reference size")
        x1, y1, x2, y2 = t["box"]
        if not all(isinstance(n, int) for n in t["box"]) or not (0 <= x1 < x2 <= size[0] and 0 <= y1 < y2 <= size[1]):
            raise ValueError("Template box outside reference frame")
        number(t["threshold"], .6, .999, "template threshold")
        path = Path(t["file"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Template file must be relative to runtime directory")
        if t.get("metric", "rgb") not in ("rgb", "white_text", "dark_text"):
            raise ValueError("Unsupported template metric")

    for t in p["templates"]:
        if t["state"] not in STATES:
            raise ValueError("Invalid template state")
        validate_region(t)
        for check in t.get("checks", []):
            validate_region(check)
        if "action" in t:
            validate_action(t["action"])
            if t["action"]["kind"] == "play" and t["state"] != "match":
                raise ValueError("play is only valid in match")
    for state, action in p["actions"].items():
        if state not in STATES:
            raise ValueError("Invalid action state")
        validate_action(action)
        if action["kind"] == "play" and state != "match":
            raise ValueError("play is only valid in match")


def readiness(cfg: dict, root: Path) -> list[str]:
    profile = cfg["profile"]
    found = {t["state"] for t in profile["templates"] if (root / t["file"]).is_file()}
    problems = [f"Missing template: {s}" for s in REQUIRED if s not in found]
    for state, action in profile["actions"].items():
        if action["kind"] == "click" and action["point"] is None:
            problems.append(f"Missing click point: {state}")
    return problems


def save(path: Path, cfg: dict) -> None:
    validate(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load(path: Path) -> dict:
    if not path.exists():
        save(path, defaults())
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("recovery", defaults()["recovery"])
    cfg["behavior"].setdefault("roaming", defaults()["behavior"]["roaming"])
    cfg["behavior"]["keys"].setdefault("backward", "S")
    # Upgrade timeouts for detailed states without changing calibrated regions.
    for state, timeout in defaults()["runtime"]["state_timeouts"].items():
        cfg["runtime"]["state_timeouts"].setdefault(state, timeout)
    validate(cfg)
    return cfg
