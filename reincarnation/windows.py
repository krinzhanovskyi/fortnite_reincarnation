from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
import threading
import time

from PIL import ImageGrab

from .config import SCANCODES

if os.name != "nt":
    raise RuntimeError("Live automation requires Windows")

user = C.WinDLL("user32", use_last_error=True)
kernel = C.WinDLL("kernel32", use_last_error=True)
ULONG_PTR = C.c_size_t


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD),
                ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]


class INPUTUNION(C.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", W.DWORD), ("data", INPUTUNION)]


user.GetForegroundWindow.restype = W.HWND
user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
user.GetClientRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
user.ClientToScreen.argtypes = [W.HWND, C.POINTER(W.POINT)]
user.IsIconic.argtypes = [W.HWND]
user.SendInput.argtypes = [W.UINT, C.POINTER(INPUT), C.c_int]
user.SendInput.restype = W.UINT
user.GetSystemMetrics.argtypes = [C.c_int]
user.RegisterHotKey.argtypes = [W.HWND, C.c_int, W.UINT, W.UINT]
user.UnregisterHotKey.argtypes = [W.HWND, C.c_int]
user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
kernel.OpenProcess.restype = W.HANDLE
kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
kernel.CloseHandle.argtypes = [W.HANDLE]


def dpi_awareness():
    try:
        user.SetProcessDpiAwarenessContext.argtypes = [C.c_void_p]
        user.SetProcessDpiAwarenessContext(C.c_void_p(-4))
    except AttributeError:
        user.SetProcessDPIAware()


@dataclass(frozen=True)
class Target:
    hwnd: int
    box: tuple[int, int, int, int]
    title: str
    process: str

    @property
    def size(self):
        return self.box[2] - self.box[0], self.box[3] - self.box[1]


def foreground(spec: dict) -> Target | None:
    hwnd = user.GetForegroundWindow()
    if not hwnd or user.IsIconic(hwnd):
        return None
    title = C.create_unicode_buffer(1024)
    user.GetWindowTextW(hwnd, title, len(title))
    if spec["title_contains"].casefold() not in title.value.casefold():
        return None
    pid = W.DWORD()
    user.GetWindowThreadProcessId(hwnd, C.byref(pid))
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return None
    try:
        path = C.create_unicode_buffer(32768)
        size = W.DWORD(len(path))
        if not kernel.QueryFullProcessImageNameW(handle, 0, path, C.byref(size)):
            return None
        process = os.path.basename(path.value)
    finally:
        kernel.CloseHandle(handle)
    if process.casefold() not in {n.casefold() for n in spec["process_names"]}:
        return None
    rect, point = W.RECT(), W.POINT(0, 0)
    if not user.GetClientRect(hwnd, C.byref(rect)) or not user.ClientToScreen(hwnd, C.byref(point)):
        return None
    if rect.right < 100 or rect.bottom < 100:
        return None
    return Target(hwnd, (point.x, point.y, point.x + rect.right, point.y + rect.bottom), title.value, process)


def capture(target: Target):
    return ImageGrab.grab(bbox=target.box, all_screens=True).convert("RGB")


def _send(event: INPUT):
    if user.SendInput(1, C.byref(event), C.sizeof(INPUT)) != 1:
        raise OSError(C.get_last_error(), "SendInput failed or was blocked")


def _key(name: str, up: bool):
    event = INPUT(type=1)
    event.ki = KEYBDINPUT(0, SCANCODES[name], 0x0008 | (0x0002 if up else 0), 0, 0)
    _send(event)


def _mouse(flags: int, dx=0, dy=0):
    event = INPUT(type=0)
    event.mi = MOUSEINPUT(dx, dy, 0, flags, 0, 0)
    _send(event)


class Input:
    """Serialized input with per-event focus/freshness checks and emergency inhibit."""

    def __init__(self, window_spec: dict, max_frame_age: float):
        self.spec, self.max_frame_age = window_spec, max_frame_age
        self.lock = threading.RLock()
        self.enabled = False
        self.keys = set()
        self.mouse_down = False
        self.pending = []
        self.target = None
        self.frame_at = float("-inf")

    def arm(self):
        with self.lock:
            self.target = None
            self.frame_at = time.monotonic()
            self.enabled = True

    def inhibit(self):
        with self.lock:
            self.enabled = False
            self.release_all()

    def frame(self, target: Target, captured_at: float):
        with self.lock:
            self.target, self.frame_at = target, captured_at

    def _guard(self):
        if not self.enabled:
            raise RuntimeError("Input is inhibited")
        current = foreground(self.spec)
        if not current or current != self.target or time.monotonic() - self.frame_at > self.max_frame_age:
            raise RuntimeError("Input blocked: focus, geometry or frame freshness changed")

    def down(self, name):
        with self.lock:
            self._guard()
            if name not in self.keys:
                # Track first so a failed send is still followed by a release attempt.
                self.keys.add(name)
                _key(name, False)

    def up(self, name):
        with self.lock:
            if name in self.keys:
                _key(name, True)
                self.keys.remove(name)

    def move(self, dx, dy):
        with self.lock:
            self._guard()
            _mouse(0x0001, dx, dy)

    def action(self, spec):
        with self.lock:
            self._guard()
            if spec["kind"] == "key":
                self.down(spec["key"])
                self.pending.append((time.monotonic() + .1, spec["key"]))
            else:
                if spec.get("point") is None:
                    raise ValueError("Uncalibrated click")
                left, top, right, bottom = self.target.box
                x = left + round(spec["point"][0] * (right - left - 1))
                y = top + round(spec["point"][1] * (bottom - top - 1))
                vx, vy = user.GetSystemMetrics(76), user.GetSystemMetrics(77)
                vw, vh = user.GetSystemMetrics(78), user.GetSystemMetrics(79)
                _mouse(0x0001 | 0x8000 | 0x4000,
                       round((x - vx) * 65535 / max(1, vw - 1)),
                       round((y - vy) * 65535 / max(1, vh - 1)))
                self._guard()
                self.mouse_down = True
                _mouse(0x0002)
                self.pending.append((time.monotonic() + .08, "mouse"))

    def flush(self, now):
        with self.lock:
            remaining = []
            for deadline, name in self.pending:
                if now >= deadline:
                    if name == "mouse":
                        if self.mouse_down:
                            _mouse(0x0004)
                            self.mouse_down = False
                    else:
                        self.up(name)
                else:
                    remaining.append((deadline, name))
            self.pending = remaining

    def release_all(self):
        with self.lock:
            failures = []
            for name in list(self.keys):
                try:
                    self.up(name)
                except OSError as exc:
                    failures.append(str(exc))
            if self.mouse_down:
                try:
                    _mouse(0x0004)
                    self.mouse_down = False
                except OSError as exc:
                    failures.append(str(exc))
            self.pending.clear()
            if failures:
                raise OSError("Could not release input: " + "; ".join(failures))


class Hotkeys:
    """Own message thread: Tk cannot consume these WM_HOTKEY messages."""

    def __init__(self, mapping, callback):
        self.mapping, self.callback = mapping, callback
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self.thread.start()
        if not self.ready.wait(3):
            self.stopped.set()
            raise RuntimeError("Hotkey registration timed out")
        if self.error:
            raise RuntimeError(self.error)

    def _run(self):
        registered = {}
        try:
            for index, (action, key) in enumerate(self.mapping.items(), 1):
                if not user.RegisterHotKey(None, index, 0x4000, 0x70 + int(key[1:]) - 1):
                    raise RuntimeError(f"Cannot register {key}; it may be used by another application")
                registered[index] = action
            self.ready.set()
            message = W.MSG()
            while not self.stopped.wait(.015):
                while user.PeekMessageW(C.byref(message), None, 0x0312, 0x0312, 1):
                    action = registered.get(message.wParam)
                    if action:
                        self.callback(action)
        except Exception as exc:
            self.error = str(exc)
            self.ready.set()
        finally:
            for index in registered:
                user.UnregisterHotKey(None, index)

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)
