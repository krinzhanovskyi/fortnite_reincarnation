"""Reviewed calibration for the user's 1920x1080 captures; never sends input.

Only the explicitly reviewed captures below are assigned states. New files are
reported as unreviewed instead of guessing a state from their filename.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import random
import shutil
import time

from PIL import Image

from .config import load, readiness, save, validate
from .vision import Recognizer
from .engine import Engine


@dataclass(frozen=True)
class Capture:
    stem: str
    state: str
    box: tuple[int, int, int, int]
    metric: str
    threshold: float
    description: str
    source_folder: str = "captures"


CAPTURES = (
    Capture("1794316162125582600", "lobby", (182, 895, 334, 927), "dark_text", .92, "Лобби, кнопка ИГРАТЬ"),
    Capture("1794316171109874600", "lobby", (182, 895, 334, 927), "dark_text", .92, "Лобби, второй вариант анимации"),
    Capture("1794316281129944700", "searching", (108, 573, 294, 601), "rgb", .94, "Поиск сервера в лобби"),
    Capture("1794316327375381000", "waiting", (845, 151, 1115, 181), "white_text", .88, "Ожидание игроков на острове"),
    Capture("1794316372814814900", "waiting", (770, 108, 1181, 131), "white_text", .88, "Обратный отсчёт отправления с острова"),
    Capture("1794316385105765400", "bus_wait", (722, 175, 1198, 193), "white_text", .90, "Автобус, высадка ещё недоступна"),
    Capture("1794316411035312200", "deploying", (1190, 591, 1674, 611), "white_text", .86, "Свободное падение, можно раскрыть дельтаплан"),
    Capture("1794316499223476800", "downed", (41, 991, 64, 1017), "white_text", .94, "Сбитый персонаж, значок X вместо здоровья"),
    Capture("1794316588178245500", "loading", (19, 996, 270, 1031), "white_text", .90, "Загрузка, оранжевый фон"),
    Capture("1794316589100030200", "loading", (19, 996, 270, 1031), "white_text", .90, "Загрузка, анимация многоточия"),
    Capture("1794316664599306900", "bus", (1190, 591, 1331, 611), "white_text", .90, "Автобус, доступна команда ВЫПРЫГНУТЬ"),
    Capture("1794316675007973800", "deploying", (1190, 591, 1674, 611), "white_text", .86, "Свободное падение, первый кадр"),
    Capture("1794316676439578100", "deploying", (1190, 591, 1674, 611), "white_text", .86, "Свободное падение, второй кадр"),
    Capture("1794316685622208500", "deploying", (1190, 591, 1515, 611), "white_text", .88, "Дельтаплан раскрыт, ждать приземления"),
    Capture("1794316736306605900", "match", (42, 993, 61, 1015), "white_text", .94, "Свой живой персонаж на земле"),
    Capture("1794317328086260800", "eliminated", (853, 14, 1063, 42), "white_text", .92, "Устранение, ждать следующего экрана"),
    Capture("1794317333882249900", "continue", (1630, 1031, 1806, 1054), "dark_text", .94, "Экран устранения с кнопкой ПРОДОЛЖИТЬ"),
    Capture("1794317343639320400", "xp", (1680, 1031, 1858, 1054), "dark_text", .94, "Начисление опыта, кнопка ПРОПУСТИТЬ"),
    Capture("1794317347554315000", "results", (1430, 468, 1735, 493), "dark_text", .94, "Итоги матча, кнопка ВЕРНУТЬСЯ В ЛОББИ"),
    Capture("1794317350498358900", "searching", (1385, 892, 1674, 930), "white_text", .90, "Поиск нового матча поверх наблюдения"),
    Capture("1794317369324810000", "loading", (89, 966, 342, 1002), "white_text", .90, "Загрузка, розовый фон"),
    Capture("1794317377816295700", "waiting", (845, 151, 1115, 181), "white_text", .88, "Остров ожидания, другое освещение"),
    Capture("1794317401059961400", "bus_wait", (722, 175, 1198, 193), "white_text", .90, "Автобус, предварительный отсчёт и баннер"),
    Capture("1794325960967508000", "spectating", (844, 17, 1073, 43), "white_text", .90,
            "Наблюдение за союзником: ожидание карты возрождения, 95 секунд", "diagnostics"),
    Capture("1794326040030763500", "spectating", (844, 17, 1073, 43), "white_text", .90,
            "Наблюдение за союзником: ожидание карты возрождения, 16 секунд"),
)

WAIT = {"kind": "wait"}


def click(x, y, delay=0):
    return {"kind": "click", "point": [x / 1919, y / 1079], "delay": delay}


def actions():
    return {"lobby": click(260, 912), "bus": {"kind": "key", "key": "SPACE"},
            "continue": click(1720, 1041, delay=6), "xp": click(1760, 1040),
            "results": click(1580, 480), "match": {"kind": "play"},
            **{s: deepcopy(WAIT) for s in ("searching", "loading", "waiting", "bus_wait",
                                         "deploying", "downed", "eliminated", "spectating")}}


def describe(action):
    kind = action["kind"]
    if kind == "wait":
        return "Отпустить ввод и ждать"
    if kind == "play":
        return "Начать эпизоды движения после задержки приземления"
    if kind == "key":
        return f"Один раз нажать {action['key']}; проверить смену экрана"
    x, y = action["point"]
    delay = f"; не ранее {action['delay']} с" if action.get("delay") else ""
    return f"Нажать ({round(x * 1919)}, {round(y * 1079)}); проверить переход{delay}"


class ReplayInput:
    """In-memory input sink. No imports of the Windows backend or OS calls."""

    def __init__(self):
        self.events = []
        self.keys = set()

    def release_all(self):
        self.keys.clear()

    def down(self, key):
        self.keys.add(key)
        self.events.append(("down", key))

    def up(self, key):
        self.keys.discard(key)
        self.events.append(("up", key))

    def move(self, dx, dy):
        self.events.append(("move", dx, dy))

    def action(self, action):
        self.events.append(("action", deepcopy(action)))


def verify_execution(cfg, obs):
    """Confirm the selected action is actually implemented, not just metadata."""
    replay_cfg = deepcopy(cfg)
    replay_cfg["runtime"]["observe_only"] = False
    output = ReplayInput()
    engine = Engine(replay_cfg, output, lambda _: None, random.Random(7))
    engine.start(0)
    early_actions = 0
    for index in range(1, 25):
        now = index * .5
        engine.tick(obs, True, now)
        if now < engine.entered + obs.action.get("delay", 0):
            early_actions += sum(e[0] == "action" for e in output.events)
    kind = obs.action["kind"]
    if kind == "wait":
        correct = not output.events and not output.keys
    elif kind == "play":
        correct = any(e[0] == "down" and e[1] == replay_cfg["behavior"]["keys"]["forward"] for e in output.events)
    else:
        correct = [e for e in output.events if e[0] == "action"] == [("action", obs.action)]
    return correct and early_actions == 0 and engine.running


def build(root: Path) -> dict:
    cfg = deepcopy(load(root / "settings.json"))
    cfg["profile"]["size"] = [1920, 1080]
    cfg["profile"]["actions"] = actions()
    templates = []
    folder = root / "templates" / "reviewed"
    folder.mkdir(parents=True, exist_ok=True)

    def region(frame, box, name, metric, threshold):
        file = f"templates/reviewed/{name}.png"
        frame.crop(box).save(root / file)
        return {"box": list(box), "file": file, "metric": metric, "threshold": threshold}

    for capture in CAPTURES:
        with Image.open(root / capture.source_folder / f"{capture.stem}.png") as im:
            frame = im.convert("RGB")
        if frame.size != (1920, 1080):
            raise ValueError(f"Unexpected capture size: {capture.stem}")
        spec = region(frame, capture.box, capture.stem, capture.metric, capture.threshold)
        spec.update(state=capture.state, action=deepcopy(cfg["profile"]["actions"][capture.state]),
                    source=f"{capture.source_folder}/{capture.stem}.png", description=capture.description)
        if capture.state in ("match", "downed"):
            # Verify the local player's HUD, not a spectator's health cross.
            spec["checks"] = [region(frame, (56, 929, 161, 948), f"{capture.stem}_own_name", "white_text", .86)]
        templates.append(spec)
    # A generic spectator label is visible in two reviewed frames. Higher
    # priority continue/searching templates determine their more specific action.
    with Image.open(root / "captures" / "1794317333882249900.png") as im:
        spec = region(im.convert("RGB"), (1523, 13, 1745, 37), "spectator_label", "white_text", .90)
    spec.update(state="spectating", action=deepcopy(WAIT), description="Режим зрителя без меню")
    templates.append(spec)
    cfg["profile"]["templates"] = templates
    validate(cfg)
    return cfg


def audit(root: Path, cfg: dict) -> dict:
    recognizer = Recognizer(cfg["profile"], root)
    entries, failures = [], []
    for capture in CAPTURES:
        with Image.open(root / capture.source_folder / f"{capture.stem}.png") as frame:
            started = time.perf_counter()
            obs = recognizer.classify(frame)
            elapsed = time.perf_counter() - started
        expected = cfg["profile"]["actions"][capture.state]
        execution_ok = bool(obs.action) and verify_execution(cfg, obs)
        ok = obs.state == capture.state and obs.action == expected and execution_ok
        entry = {"file": f"{capture.source_folder}/{capture.stem}.png", "expected": capture.state,
                 "actual": obs.state, "score": round(obs.score, 4), "action": obs.action,
                 "description": capture.description, "ok": ok, "execution_ok": execution_ok,
                 "milliseconds": round(elapsed * 1000, 2)}
        entries.append(entry)
        if not ok:
            failures.append(entry)
    reviewed = {f"{c.stem}.png" for c in CAPTURES}
    return {"reviewed": len(entries), "passed": len(entries) - len(failures),
            "unreviewed": [p.name for p in sorted((root / "captures").glob("*.png")) if p.name not in reviewed],
            "readiness": readiness(cfg, root), "entries": entries}


def write_report(root: Path, report: dict):
    folder = root / "calibration"
    folder.mkdir(exist_ok=True)
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Калибровка сохранённых кадров", "",
             f"Верно распознано: {report['passed']}/{report['reviewed']}. Проверка без игрового ввода.", "",
             "Это проверка размеченных кадров; устойчивость на новых кадрах проверяется отдельно.", "",
             "| Снимок | Состояние | Действие | Проверка |", "|---|---|---|---|"]
    for e in report["entries"]:
        action = describe(e["action"]) if e["action"] else "Не определено"
        lines.append(f"| [{Path(e['file']).name}](../{e['file']}) — {e['description']} | {e['actual']} | {action} | {'OK' if e['ok'] else 'ОШИБКА'} |")
    if report["unreviewed"]:
        lines.extend(["", "Новые неразмеченные кадры: " + ", ".join(report["unreviewed"])])
    lines.extend(["", "Все ожидания имеют тайм-аут. Меню-действия имеют ограничение повторов.",
                  "Режим наблюдения не изменяется калибровкой."])
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--apply", action="store_true", help="Apply only after every reviewed state/action passes")
    args = parser.parse_args()
    root = args.data_dir.resolve()
    cfg = build(root)
    report = audit(root, cfg)
    write_report(root, report)
    save(root / "calibration" / "settings.calibrated.json", cfg)
    for e in report["entries"]:
        print(f"{'OK' if e['ok'] else 'FAIL'} {Path(e['file']).name}: {e['expected']} -> {e['actual']} ({e['score']:.3f})")
    if report["passed"] != report["reviewed"] or report["readiness"]:
        print("Not applied: calibration checks failed")
        return 2
    if args.apply:
        backup = root / "calibration" / f"settings.backup.{time.time_ns()}.json"
        shutil.copy2(root / "settings.json", backup)
        save(root / "settings.json", cfg)
        print(f"Applied. Backup: {backup}")
    print(f"Report: {root / 'calibration' / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
