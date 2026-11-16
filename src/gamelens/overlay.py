"""点击穿透叠加层：把译文"画"在游戏画面上，但不碰游戏的任何输入。

关键点（这是"能一边看汉化一边继续玩"的全部秘密）：
1. 顶层窗口 + 无边框 + 置顶，覆盖在游戏画面之上；
2. WS_EX_TRANSPARENT：鼠标事件完全穿透到游戏窗口，点击按钮仍然命中游戏；
3. WS_EX_NOACTIVATE：永不抢焦点，键盘输入照常进游戏；
4. -transparentcolor 色键抠图：只有文字/色块区域不透明，其余像素完全透明。
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from . import winapi

CHROMA = "#010203"          # 色键色，画布底色即"完全透明"


@dataclass
class Sprite:
    box: Tuple[int, int, int, int]     # 相对叠加层左上角
    text: str
    bg: str = "#101014"
    fg: str = "#f2f2f2"
    bold: bool = False
    align: str = "center"
    outline: Optional[str] = None


def _round_rect(cv: tk.Canvas, x: float, y: float, w: float, h: float, r: float, **kw):
    r = max(0.0, min(r, w / 2, h / 2))
    pts = [
        x + r, y, x + w - r, y, x + w, y, x + w, y + r,
        x + w, y + h - r, x + w - r, y + h, x + r, y + h,
        x, y + h, x, y + h - r, x, y + r, x + r, y,
    ]
    return cv.create_polygon(pts, smooth=True, splinesteps=8, **kw)


class OverlayWindow:
    """一块可移动、可缩放的点击穿透画布。"""

    def __init__(self, master: tk.Misc, font_family: str = "Microsoft YaHei UI",
                 font_scale: float = 0.62, mask: bool = True, mask_pad: int = 2,
                 stroke: bool = False, stipple: str = "") -> None:
        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            pass
        self.win.configure(bg=CHROMA)
        self.canvas = tk.Canvas(self.win, bg=CHROMA, highlightthickness=0, bd=0,
                                takefocus=0, cursor="none")
        self.canvas.pack(fill="both", expand=True)
        self._hwnd = 0
        self.font_family = font_family
        self.font_scale = font_scale
        self.mask = mask
        self.mask_pad = mask_pad
        self.stroke = stroke
        self.stipple = stipple
        self.geometry: Tuple[int, int, int, int] = (0, 0, 0, 0)
        self._fonts: dict = {}
        self.visible = False
        self._last_payload: Optional[List[Sprite]] = None

    # ------------------------------------------------------------ 窗口
    def show(self) -> None:
        if not self.visible:
            self.win.deiconify()
            self.visible = True
        self._apply_click_through()
        self.win.lift()
        winapi.keep_topmost(self._hwnd or winapi.hwnd_of(self.win), True)

    def hide(self) -> None:
        if self.visible:
            self.win.withdraw()
            self.visible = False
            self._last_payload = None

    def set_geometry(self, bbox: Tuple[int, int, int, int]) -> None:
        x, y, w, h = bbox
        if w <= 0 or h <= 0:
            return
        self.geometry = (x, y, w, h)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.canvas.configure(width=w, height=h)

    def _apply_click_through(self) -> None:
        self.win.update_idletasks()
        hwnd = winapi.hwnd_of(self.win)
        if hwnd and hwnd != self._hwnd:
            self._hwnd = hwnd
            winapi.make_click_through(hwnd)

    # ------------------------------------------------------------ 渲染
    def _font(self, px: int, bold: bool = False) -> tkfont.Font:
        key = (px, bold)
        f = self._fonts.get(key)
        if f is None:
            f = tkfont.Font(family=self.font_family, size=-max(6, int(px)),
                            weight="bold" if bold else "normal")
            self._fonts[key] = f
        return f

    def render(self, sprites: Sequence[Sprite]) -> None:
        cv = self.canvas
        cv.delete("all")
        pad = max(1, int(self.mask_pad))
        # 同一帧里字号统一：按"中等行高"定基准，避免 OCR 框高低不一时
        # 有的译文特别大、有的小到看不清
        heights = sorted(sp.box[3] for sp in sprites if sp.box[3] > 3)
        ref_h = heights[len(heights) // 2] if heights else 0
        for sp in sprites:
            if not sp.text.strip():
                continue
            x, y, w, h = sp.box
            if w < 4 or h < 4:
                continue
            if self.mask:
                # 横向多留一点：OCR 框往往比肉眼看到的文字窄，底色不够会露出原文边角
                px_, py_ = max(pad, int(w * 0.035)), max(pad, int(h * 0.18))
                _round_rect(cv, x - px_, y - py_, w + px_ * 2, h + py_ * 2,
                            r=min(6, (h + py_) * 0.28), fill=sp.bg, outline=sp.bg,
                            stipple=self.stipple or "")
            base_h = ref_h or h
            if h > base_h * 1.6:      # 真正的大标题保持大字号
                base_h = h
            px = max(11, min(72, int(base_h * self.font_scale / 0.75)))
            px = self._fit(sp.text, w + pad, px)
            cx = x + w / 2
            cy = y + h / 2
            kw = dict(text=sp.text, fill=sp.fg, font=self._font(px, sp.bold),
                      anchor="center", justify="center", width=max(8, w - pad * 2))
            if self.stroke:
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    cv.create_text(cx + dx, cy + dy, fill=sp.bg, **kw)
            cv.create_text(cx, cy, **kw)
        self._last_payload = list(sprites)

    def redraw(self) -> None:
        if self._last_payload:
            self.render(self._last_payload)

    def clear(self) -> None:
        self.canvas.delete("all")
        self._last_payload = []

    def _fit(self, text: str, max_w: int, start_px: int, min_px: int = 11) -> int:
        for px in range(max(min_px, int(start_px)), min_px - 1, -1):
            if self._font(px).measure(text) <= max_w:
                return px
        return min_px

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except Exception:
            pass
