"""全屏框选器：暗化桌面 + 拖拽选区，返回物理像素坐标。"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional, Tuple

from PIL import Image, ImageEnhance, ImageTk

from . import capture, winapi
from .theme import ACCENT, BG, FG, MUTED


class RegionSelector:
    def __init__(self, master: tk.Misc,
                 on_done: Callable[[Tuple[int, int, int, int]], None],
                 on_cancel: Optional[Callable[[], None]] = None) -> None:
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.vx, self.vy, self.vw, self.vh = winapi.virtual_screen_rect()

        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{self.vw}x{self.vh}+{self.vx}+{self.vy}")
        self.win.configure(bg=BG)
        self.canvas = tk.Canvas(self.win, bg=BG, highlightthickness=0, bd=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self._photo = None
        self._start: Optional[Tuple[int, int]] = None
        self._rect = None
        self._label = None
        self._cross_h = None
        self._cross_v = None
        self._bbox: Optional[Tuple[int, int, int, int]] = None

        self._load_background()
        self._draw_hint()

        c = self.canvas
        c.bind("<Button-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Motion>", self._on_move)
        self.win.bind("<Escape>", lambda e: self._cancel())
        self.win.bind("<Button-3>", lambda e: self._cancel())
        self.win.bind("<Return>", lambda e: self._confirm())
        self.win.focus_force()
        try:
            self.win.grab_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    def _load_background(self) -> None:
        try:
            img = capture.grab((self.vx, self.vy, self.vw, self.vh))
            img = ImageEnhance.Brightness(img).enhance(0.42)
            self._photo = ImageTk.PhotoImage(img)
            self.canvas.create_image(0, 0, anchor="nw", image=self._photo, tags="bg")
        except Exception:
            self.canvas.configure(bg="#0a0b0d")

    def _draw_hint(self) -> None:
        w = self.vw
        self.canvas.create_rectangle(w / 2 - 250, 22, w / 2 + 250, 62,
                                     fill="#14161a", outline=ACCENT, width=1)
        self.canvas.create_text(w / 2, 42,
                                text="拖动鼠标框选需要汉化的区域　·　松开完成　·　ESC 取消",
                                fill=FG, font=("Microsoft YaHei UI", -15))

    # ------------------------------------------------------------------
    def _on_press(self, e) -> None:
        self._start = (e.x, e.y)
        if self._rect:
            self.canvas.delete(self._rect)
            self._rect = None
        if self._label:
            self.canvas.delete(self._label)
            self._label = None

    def _on_drag(self, e) -> None:
        if not self._start:
            return
        x0, y0 = self._start
        x1, y1 = e.x, e.y
        if self._rect:
            self.canvas.coords(self._rect, x0, y0, x1, y1)
        else:
            self._rect = self.canvas.create_rectangle(
                x0, y0, x1, y1, outline=ACCENT, width=2, dash=(5, 3))
        w, h = abs(x1 - x0), abs(y1 - y0)
        tx, ty = min(x1, x0) + 8, max(10, min(y0, y1) - 14)
        if self._label:
            self.canvas.itemconfigure(self._label, text=f"{w} × {h}")
            self.canvas.coords(self._label, tx, ty)
        else:
            self._label = self.canvas.create_text(tx, ty, text=f"{w} × {h}",
                                                  anchor="nw", fill=ACCENT,
                                                  font=("Consolas", -14))

    def _on_move(self, e) -> None:
        if self._cross_h is None:
            self._cross_h = self.canvas.create_line(0, e.y, self.vw, e.y,
                                                    fill="#6b7280", dash=(2, 6))
            self._cross_v = self.canvas.create_line(e.x, 0, e.x, self.vh,
                                                    fill="#6b7280", dash=(2, 6))
        else:
            self.canvas.coords(self._cross_h, 0, e.y, self.vw, e.y)
            self.canvas.coords(self._cross_v, e.x, 0, e.x, self.vh)

    def _on_release(self, e) -> None:
        if not self._start:
            return
        x0, y0 = self._start
        x1, y1 = e.x, e.y
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w < 12 or h < 12:
            self._start = None
            return
        self._bbox = (min(x0, x1) + self.vx, min(y0, y1) + self.vy, w, h)
        self._confirm()

    def _confirm(self) -> None:
        if not self._bbox:
            return
        bbox = self._bbox
        self._close()
        self.on_done(bbox)

    def _cancel(self) -> None:
        self._close()
        if self.on_cancel:
            self.on_cancel()

    def _close(self) -> None:
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
