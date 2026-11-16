"""全局热键：在游戏窗口保持前台时也能开关汉化。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
from typing import Callable, Dict, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "ESC": 0x1B, "TAB": 0x09, "SPACE": 0x20,
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45, "F": 0x46,
    "G": 0x47, "H": 0x48, "I": 0x49, "J": 0x4A, "K": 0x4B, "L": 0x4C,
    "M": 0x4D, "N": 0x4E, "O": 0x4F, "P": 0x50, "Q": 0x51, "R": 0x52,
    "S": 0x53, "T": 0x54, "U": 0x55, "V": 0x56, "W": 0x57, "X": 0x58,
    "Y": 0x59, "Z": 0x5A,
}


class HotkeyManager:
    """在独立线程里跑消息循环，回调在后台线程触发（调用方需自行切回主线程）。"""

    def __init__(self) -> None:
        self._bindings: Dict[int, tuple] = {}
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.failed: list = []
        self._next_id = 1

    def register(self, spec: str, callback: Callable[[], None]) -> bool:
        """spec 形如 'F8' 或 'CTRL+F8'。"""
        mods = MOD_NOREPEAT
        key = spec.upper()
        for name, flag in (("CTRL", MOD_CONTROL), ("ALT", MOD_ALT),
                           ("SHIFT", MOD_SHIFT), ("WIN", MOD_WIN)):
            if key.startswith(name + "+"):
                mods |= flag
                key = key[len(name) + 1:]
        vk = VK.get(key) or (ord(key[0]) if len(key) == 1 else 0)
        if not vk:
            self.failed.append(spec)
            return False
        hid = self._next_id
        self._next_id += 1
        self._bindings[hid] = (mods, vk, callback, spec)
        return True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="gamelens-hotkey",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=1.0)

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        ok_all = []
        for hid, (mods, vk, cb, spec) in self._bindings.items():
            if user32.RegisterHotKey(None, hid, mods, vk):
                ok_all.append(spec)
            else:
                self.failed.append(spec)
        self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                entry = self._bindings.get(int(msg.wParam))
                if entry:
                    try:
                        entry[2]()
                    except Exception:
                        pass
        for hid in self._bindings:
            user32.UnregisterHotKey(None, hid)

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread = None
        self._thread_id = 0

    def registered(self) -> list:
        return [b[3] for b in self._bindings.values() if b[3] not in self.failed]
