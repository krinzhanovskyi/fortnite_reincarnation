from __future__ import annotations

import json
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageTk

from .config import REQUIRED, STATES, load, readiness, save, validate
from .vision import Recognizer
from .windows import Hotkeys
from .worker import Worker

LABELS = {"unknown": "Неизвестно", "lobby": "Лобби", "searching": "Поиск",
          "loading": "Загрузка", "waiting": "Остров ожидания", "bus": "Автобус",
          "deploying": "Высадка / спуск", "match": "Матч", "results": "Результаты",
          "bus_wait": "Ожидание высадки", "downed": "Персонаж сбит",
          "eliminated": "Устранён", "spectating": "Наблюдение",
          "continue": "Продолжить после матча", "xp": "Начисление опыта"}


class Calibration(tk.Toplevel):
    def __init__(self, app, image, source):
        super().__init__(app.window)
        self.app, self.image, self.source = app, image.convert("RGB"), source
        self.title("Калибровка — выделите стабильную часть интерфейса")
        self.geometry("1120x840")
        self.box = None
        self.point = None
        self.anchor = None
        self.state = tk.StringVar(value="lobby")
        self.mode = tk.StringVar(value="region")
        self.threshold = tk.DoubleVar(value=.94)
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Label(bar, text="Состояние:").pack(side="left")
        ttk.Combobox(bar, textvariable=self.state, values=STATES, state="readonly", width=12).pack(side="left", padx=5)
        ttk.Radiobutton(bar, text="Область распознавания", variable=self.mode, value="region").pack(side="left")
        ttk.Radiobutton(bar, text="Точка нажатия", variable=self.mode, value="point").pack(side="left")
        ttk.Label(bar, text="Порог:").pack(side="left", padx=8)
        ttk.Spinbox(bar, from_=.6, to=.999, increment=.005, textvariable=self.threshold, width=6).pack(side="left")
        self.scale = min(1040 / self.image.width, 560 / self.image.height, 1)
        preview = self.image.resize((round(self.image.width * self.scale), round(self.image.height * self.scale)))
        self.photo = ImageTk.PhotoImage(preview)
        self.canvas = tk.Canvas(self, width=preview.width, height=preview.height, bg="#161b22", highlightthickness=0)
        self.canvas.pack(pady=4)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.info = tk.StringVar(value=f"{source.name} | {self.image.width}×{self.image.height}")
        ttk.Label(self, textvariable=self.info).pack(fill="x", padx=12)
        ttk.Label(self, text="Выделите небольшой статичный текст/значок. Не включайте таймер, имя игрока и игровой фон.", wraplength=1000).pack(pady=4)
        buttons = ttk.Frame(self, padding=6)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Добавить шаблон области", command=self.add_template).pack(side="left", padx=4)
        ttk.Button(buttons, text="Сохранить точку меню", command=self.save_point).pack(side="left", padx=4)
        ttk.Button(buttons, text="Проверить этот кадр", command=self.test_frame).pack(side="left", padx=4)
        ttk.Button(buttons, text="Удалить шаблоны состояния", command=self.clear_state).pack(side="left", padx=4)
        self.checklist = tk.StringVar()
        ttk.Label(self, textvariable=self.checklist, wraplength=1060).pack(fill="x", padx=12, pady=8)
        self.refresh()
        # A capture is explicitly requested while the game is foreground. Make
        # its editor visible rather than leaving it behind the fullscreen window.
        self.after_idle(self.show_editor)

    def show_editor(self):
        if not self.winfo_exists():
            return
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.focus_force()
        self.after(400, lambda: self.attributes("-topmost", False) if self.winfo_exists() else None)

    def coords(self, event):
        return (min(self.image.width - 1, max(0, round(event.x / self.scale))),
                min(self.image.height - 1, max(0, round(event.y / self.scale))))

    def press(self, event):
        self.anchor = self.coords(event)
        if self.mode.get() == "point":
            self.point = self.anchor
            self.canvas.delete("point")
            x, y = [v * self.scale for v in self.point]
            self.canvas.create_line(x - 9, y, x + 9, y, fill="#ffcf40", width=2, tags="point")
            self.canvas.create_line(x, y - 9, x, y + 9, fill="#ffcf40", width=2, tags="point")
            self.info.set(f"Точка: {self.point}")

    def drag(self, event):
        if self.mode.get() != "region" or self.anchor is None:
            return
        x, y = self.coords(event)
        ax, ay = self.anchor
        self.box = (min(ax, x), min(ay, y), max(ax, x), max(ay, y))
        self.canvas.delete("region")
        self.canvas.create_rectangle(*(v * self.scale for v in self.box), outline="#35e5a0", width=2, tags="region")
        self.info.set(f"Область: {self.box}")

    def release(self, event):
        self.drag(event)
        self.anchor = None

    def config(self):
        cfg = load(self.app.worker.config_path)
        size = cfg["profile"]["size"]
        if size is not None and tuple(size) != self.image.size:
            raise ValueError("Размер отличается от профиля. Верните разрешение игры или создайте другой --data-dir.")
        cfg["profile"]["size"] = list(self.image.size)
        return cfg

    def add_template(self):
        try:
            if not self.box or self.box[2] - self.box[0] < 12 or self.box[3] - self.box[1] < 8:
                raise ValueError("Выделите прямоугольную область не меньше 12×8 пикселей")
            cfg = self.config()
            threshold = float(self.threshold.get())
            name = f"templates/{self.state.get()}_{time.time_ns()}.png"
            spec = {"state": self.state.get(), "box": list(self.box), "file": name, "threshold": threshold}
            if self.point is not None and self.state.get() in ("lobby", "bus", "continue", "xp", "results"):
                spec["action"] = {"kind": "click", "point": [self.point[0] / max(1, self.image.width - 1), self.point[1] / max(1, self.image.height - 1)]}
                spec["action"]["delay"] = cfg["profile"]["actions"].get(self.state.get(), {}).get("delay", 0)
            cfg["profile"]["templates"].append(spec)
            validate(cfg)
            path = self.app.root / name
            path.parent.mkdir(exist_ok=True)
            self.image.crop(self.box).save(path)
            save(self.app.worker.config_path, cfg)
            self.info.set(f"Шаблон сохранён: {name}")
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Калибровка", str(exc), parent=self)

    def save_point(self):
        try:
            state = self.state.get()
            if state not in ("lobby", "bus", "continue", "xp", "results"):
                raise ValueError("Точки действий используются для lobby, bus, continue, xp и results")
            if self.point is None:
                raise ValueError("Выберите режим «Точка нажатия» и нажмите на нужную кнопку в снимке")
            cfg = self.config()
            cfg["profile"]["actions"][state] = {"kind": "click", "point": [self.point[0] / max(1, self.image.width - 1), self.point[1] / max(1, self.image.height - 1)]}
            if state == "continue":
                cfg["profile"]["actions"][state]["delay"] = 6
            save(self.app.worker.config_path, cfg)
            self.info.set(f"Точка {state} сохранена")
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Калибровка", str(exc), parent=self)

    def clear_state(self):
        cfg = load(self.app.worker.config_path)
        cfg["profile"]["templates"] = [t for t in cfg["profile"]["templates"] if t["state"] != self.state.get()]
        save(self.app.worker.config_path, cfg)
        self.refresh()

    def test_frame(self):
        try:
            cfg = load(self.app.worker.config_path)
            obs = Recognizer(cfg["profile"], self.app.root).classify(self.image)
            self.info.set(f"Результат: {obs.state}, сходство {obs.score:.3f}, {obs.detail}")
        except Exception as exc:
            messagebox.showerror("Проверка", str(exc), parent=self)

    def refresh(self):
        cfg = load(self.app.worker.config_path)
        counts = {s: sum(t["state"] == s for t in cfg["profile"]["templates"]) for s in STATES}
        missing = readiness(cfg, self.app.root)
        self.checklist.set("Шаблоны: " + ", ".join(f"{s}: {n}" for s, n in counts.items()) +
                           "\n" + ("Не готово: " + "; ".join(missing) if missing else "Профиль заполнен. Проверьте распознавание на других кадрах."))


class App:
    def __init__(self, root: Path):
        self.root = root
        self.window = tk.Tk()
        self.window.title("Fortnite Reincarnation")
        self.window.geometry("900x670")
        self.events = queue.Queue()
        self.worker = Worker(root, self.events)
        self.window.report_callback_exception = self.callback_error
        self.closing = False
        self.hk = None
        self.status = tk.StringVar(value="Пауза")
        self.detail = tk.StringVar(value="")
        self.mode = tk.BooleanVar(value=self.worker.cfg["runtime"]["observe_only"])
        self.controls = []
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Fortnite Reincarnation", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        self.hotkey_label = ttk.Label(frame)
        self.hotkey_label.pack(anchor="w", pady=6)
        ttk.Label(frame, textvariable=self.status, font=("Segoe UI", 13)).pack(anchor="w", pady=6)
        ttk.Label(frame, textvariable=self.detail, wraplength=850).pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=12)
        ttk.Label(buttons, text="Для запуска: откройте игру и нажмите горячую клавишу старта.").pack(anchor="w")
        ttk.Button(buttons, text="Пауза", command=lambda: self.worker.request("pause")).pack(side="left", pady=8)
        ttk.Button(buttons, text="НЕМЕДЛЕННЫЙ СТОП", command=lambda: self.worker.request("stop")).pack(side="left", padx=8)
        c = ttk.Checkbutton(frame, text="Только наблюдение — без нажатий и движений мыши", variable=self.mode, command=self.save_mode)
        c.pack(anchor="w")
        self.controls.append(c)
        tools = ttk.Frame(frame)
        tools.pack(fill="x", pady=12)
        for label, callback in (("Последний снимок", self.open_latest),
                                ("Открыть снимок для калибровки", self.open_image),
                                ("Настройки JSON", self.settings), ("Проверить профиль", self.check_profile)):
            button = ttk.Button(tools, text=label, command=callback)
            button.pack(side="left", padx=(0, 6))
            self.controls.append(button)
        ttk.Label(frame, text="Снимок: на паузе нажмите F9 в окне игры. Исходные снимки и журнал сохраняются в runtime.", wraplength=850).pack(anchor="w")
        self.log = ScrolledText(frame, height=17, state="disabled", font=("Consolas", 10))
        self.log.pack(fill="both", expand=True, pady=8)
        ttk.Label(frame, text=str(root)).pack(anchor="w")
        self.register_hotkeys()
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.after(40, self.pump)

    def register_hotkeys(self):
        if self.hk:
            self.hk.close()
            self.hk = None
        cfg = load(self.worker.config_path)
        try:
            self.hk = Hotkeys(cfg["hotkeys"], self.worker.request)
            self.hotkey_label.configure(text=" | ".join(f"{cfg['hotkeys'][a]} — {name}" for a, name in
                (("start", "старт"), ("pause", "пауза"), ("stop", "стоп"), ("capture", "снимок"))))
        except Exception:
            self.worker.request("stop")
            raise

    def paused(self):
        if self.worker.engine.running:
            messagebox.showinfo("Пауза", "Сначала поставьте бота на паузу.", parent=self.window)
            return False
        return True

    def save_mode(self):
        if not self.paused():
            self.mode.set(self.worker.cfg["runtime"]["observe_only"])
            return
        cfg = load(self.worker.config_path)
        cfg["runtime"]["observe_only"] = self.mode.get()
        save(self.worker.config_path, cfg)

    def open_image(self):
        if not self.paused():
            return
        path = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.bmp")], parent=self.window)
        if path:
            try:
                with Image.open(path) as im:
                    image = im.convert("RGB")
                Calibration(self, image, Path(path))
            except Exception as exc:
                messagebox.showerror("Снимок", str(exc), parent=self.window)

    def open_latest(self):
        if not self.paused():
            return
        paths = sorted((self.root / "captures").glob("*.png"), key=lambda p: p.stat().st_mtime_ns)
        if not paths:
            messagebox.showinfo("Снимок", "Снимков пока нет. Перейдите в Fortnite и нажмите F9 на паузе.", parent=self.window)
            return
        with Image.open(paths[-1]) as im:
            image = im.convert("RGB")
        Calibration(self, image, paths[-1])

    def callback_error(self, exc_type, exc_value, traceback):
        self.worker.request("recover")
        self.worker.logger.error("Interface error", exc_info=(exc_type, exc_value, traceback))
        messagebox.showerror("Ошибка интерфейса", str(exc_value), parent=self.window)
        # A Tk callback error must not permanently stop delivery of worker events.
        if not self.closing:
            self.window.after(100, self.pump)

    def check_profile(self):
        cfg = load(self.worker.config_path)
        problems = readiness(cfg, self.root)
        messagebox.showinfo("Профиль", "\n".join(problems) if problems else "Все обязательные шаблоны и точки заданы. Проверьте их в режиме наблюдения.", parent=self.window)

    def settings(self):
        if not self.paused():
            return
        editor = tk.Toplevel(self.window)
        editor.title("Настройки — применяются при следующем старте")
        editor.geometry("820x720")
        text = ScrolledText(editor, font=("Consolas", 10))
        text.pack(fill="both", expand=True)
        text.insert("1.0", json.dumps(load(self.worker.config_path), ensure_ascii=False, indent=2))

        def apply():
            try:
                if not self.paused():
                    return
                cfg = json.loads(text.get("1.0", "end"))
                validate(cfg)
                Recognizer(cfg["profile"], self.root)
                previous = load(self.worker.config_path)
                # Register hotkeys while paused; roll back settings on conflicts.
                save(self.worker.config_path, cfg)
                try:
                    self.register_hotkeys()
                except Exception:
                    save(self.worker.config_path, previous)
                    self.register_hotkeys()
                    raise
                self.mode.set(cfg["runtime"]["observe_only"])
                editor.destroy()
            except Exception as exc:
                messagebox.showerror("Настройки", str(exc), parent=editor)

        ttk.Button(editor, text="Проверить и сохранить", command=apply).pack(pady=8)

    def append_log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + line + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 1500:
            self.log.delete("1.0", "300.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def pump(self):
        if self.closing:
            return
        if self.hk and self.hk.error:
            self.worker.request("stop")
            self.append_log("HOTKEY ERROR: " + self.hk.error)
            self.hk.close()
            self.hk = None
            self.hotkey_label.configure(text="Горячие клавиши отключились. Перезапустите приложение.")
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(value)
            elif kind == "status":
                mode = "НАБЛЮДЕНИЕ" if value["observing"] else "УПРАВЛЕНИЕ"
                self.status.set(f"{'Работает' if value['running'] else 'Пауза'} | {mode} | {LABELS[value['state']]} | Матчей: {value['cycles']}")
                self.detail.set(f"{value['reason']} | Кадр: {value['observed']}, {value['score']:.3f} ({value['detail']}) | Действие: {value['action']} | Перезапуски: {value['restarts']}")
                for control in self.controls:
                    control.configure(state="disabled" if value["running"] or value["recovering"] else "normal")
            elif kind == "capture":
                Calibration(self, *value)
                self.append_log("Снимок готов. Окно «Калибровка» открыто; также доступна кнопка «Последний снимок».")
            elif kind == "error":
                self.append_log("ERROR: " + value)
        self.window.after(40, self.pump)

    def close(self):
        self.closing = True
        self.worker.request("stop")
        if self.hk:
            self.hk.close()
        self.worker.close()
        self.window.destroy()

    def run(self):
        try:
            self.window.mainloop()
        finally:
            if not self.closing:
                self.close()
