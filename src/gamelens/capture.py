"""屏幕抓取、帧变化检测与配色采样。"""
from __future__ import annotations

import hashlib
from typing import Optional, Tuple

from PIL import Image, ImageGrab

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

Box = Tuple[int, int, int, int]


def grab(bbox: Optional[Box] = None) -> Image.Image:
    """抓取屏幕（物理像素）。bbox 为 (x, y, w, h)，None 表示整个虚拟桌面。"""
    if bbox is None:
        return ImageGrab.grab(all_screens=True)
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        raise ValueError("截图区域无效")
    return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)


def grab_window(hwnd: int, bbox: Optional[Box] = None) -> Optional[Image.Image]:
    """直接抓取某个窗口的客户区内容（PrintWindow + PW_RENDERFULLCONTENT）。

    相比抓屏，它有两个关键好处：
      1. 不会把自己画的译文叠加层再抓进来（否则 OCR 会读到自己写的字，越翻越乱）；
      2. 窗口被别的窗口挡住、或不在最前面，也照样抓得到。
    抓不到（部分 D3D 独占画面会返回全黑）时返回 None，由调用方退回抓屏。
    """
    import ctypes
    import ctypes.wintypes as wt

    from . import winapi

    u32, g32 = winapi.user32, ctypes.windll.gdi32
    wr = winapi.window_rect(hwnd)
    cr = winapi.client_rect_on_screen(hwnd)
    if not wr or not cr:
        return None
    w, h = wr[2], wr[3]
    if w <= 0 or h <= 0:
        return None

    hdc = u32.GetWindowDC(wt.HWND(hwnd))
    if not hdc:
        return None
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    old = g32.SelectObject(mdc, bmp)

    class BMI(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    img = None
    try:
        ok = u32.PrintWindow(wt.HWND(hwnd), mdc, 2)   # PW_RENDERFULLCONTENT
        if not ok:
            return None
        bmi = BMI()
        bmi.biSize = ctypes.sizeof(BMI)
        bmi.biWidth, bmi.biHeight = w, -h
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        if not g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
            return None
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
    except Exception:
        return None
    finally:
        try:
            g32.SelectObject(mdc, old)
            g32.DeleteObject(bmp)
            g32.DeleteDC(mdc)
            u32.ReleaseDC(wt.HWND(hwnd), hdc)
        except Exception:
            pass

    # 客户区在窗口内的偏移（去掉标题栏与边框）
    ox, oy = cr[0] - wr[0], cr[1] - wr[1]
    img = img.crop((ox, oy, ox + cr[2], oy + cr[3]))
    if bbox:
        cx = bbox[0] - cr[0]
        cy = bbox[1] - cr[1]
        img = img.crop((cx, cy, cx + bbox[2], cy + bbox[3]))
    extrema = img.convert("L").getextrema()
    if extrema[1] - extrema[0] < 4:      # 全黑/全白 = 抓不到真实画面
        return None
    return img


class FrameHasher:
    """把画面缩到很小再做哈希，用来快速判断"这一帧变了没有"。"""

    def __init__(self, size: Tuple[int, int] = (56, 32)) -> None:
        self.size = size
        self._last: Optional[str] = None

    def digest(self, img: Image.Image) -> str:
        small = img.convert("L").resize(self.size, Image.BILINEAR)
        return hashlib.blake2b(small.tobytes(), digest_size=8).hexdigest()

    def changed(self, img: Image.Image, force: bool = False) -> bool:
        d = self.digest(img)
        if force or d != self._last:
            self._last = d
            return True
        return False


def _luma(c: Tuple[int, int, int]) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def analyze_colors(img: Image.Image, box: Box) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """采样文本框的背景色与文字色，用于让译文"长"得和原界面一致。

    返回 (bg, fg)。bg 取框内偏暗像素的中位数（排除文字笔画），
    fg 取偏亮像素的均值，再用对比度规则兜底。
    """
    x, y, w, h = box
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(img.width, x + w), min(img.height, y + h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return (16, 16, 20), (245, 245, 245)

    crop = img.crop((x0, y0, x1, y1)).convert("RGB")
    if np is not None:
        arr = np.asarray(crop, dtype=np.uint8).reshape(-1, 3)
        lum = (arr[:, 0] * 0.2126 + arr[:, 1] * 0.7152 + arr[:, 2] * 0.0722)
        lo = lum <= max(12.0, float(np.percentile(lum, 62)))
        hi = lum >= float(np.percentile(lum, 96))
        bg = np.median(arr[lo], axis=0) if lo.any() else np.median(arr, axis=0)
        fg = np.mean(arr[hi], axis=0) if hi.any() else np.array([240, 240, 240])
        bg_t = (int(bg[0]), int(bg[1]), int(bg[2]))
        fg_t = (int(fg[0]), int(fg[1]), int(fg[2]))
    else:  # 纯 PIL 兜底
        colors = crop.getcolors(maxcolors=1 << 20) or []
        colors.sort(key=lambda c: c[0], reverse=True)
        bg_t = colors[0][1] if colors else (16, 16, 20)
        fg_t = (240, 240, 240)

    # 对比度兜底：保证文字可读
    if abs(_luma(fg_t) - _luma(bg_t)) < 80:
        fg_t = (250, 250, 250) if _luma(bg_t) < 128 else (18, 18, 20)
    return bg_t, fg_t


def hexc(c: Tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % (max(0, min(255, c[0])),
                              max(0, min(255, c[1])),
                              max(0, min(255, c[2])))


def blend(c: Tuple[int, int, int], other: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    return (int(c[0] + (other[0] - c[0]) * t),
            int(c[1] + (other[1] - c[1]) * t),
            int(c[2] + (other[2] - c[2]) * t))
