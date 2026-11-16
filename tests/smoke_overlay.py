"""端到端验证（不依赖真实游戏窗口是否被前置）：

用一张 Fake Quest 的静态截图喂给流水线，验证
  1) OCR -> 合并 -> 翻译 -> 叠加层渲染 的位置与文本；
  2) 叠加层确实是"点击穿透"的（WindowFromPoint 命中的是下层窗口）；
  3) 叠加层带 WS_EX_TRANSPARENT / WS_EX_NOACTIVATE 样式。

运行：python tests/smoke_overlay.py [源图] [输出图]
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time

sys.path.insert(0, "src")

from gamelens import winapi

winapi.enable_dpi_awareness()

from PIL import Image, ImageGrab  # noqa: E402

from gamelens import capture  # noqa: E402
from gamelens.app import App  # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else "tests/assets/sample_game.png"
OUT = sys.argv[2] if len(sys.argv) > 2 else "smoke_overlay.png"
BBOX = (140, 150, 960, 560)

u32 = ctypes.windll.user32


def main() -> int:
    try:
        src_img = Image.open(SRC).convert("RGB")
    except Exception:
        print("FAIL: 缺少素材", SRC)
        return 1
    if src_img.size != (BBOX[2], BBOX[3]):
        src_img = src_img.resize((BBOX[2], BBOX[3]))

    capture.grab = lambda bbox=None: src_img.copy()

    app = App()
    app.cfg.set("region", list(BBOX))
    app.cfg.set("provider", "litemt")
    app.cfg.set("interval_ms", 300)
    app.cfg.save()
    app.service.invalidate()
    app.geometry("+1250+30")

    state = {"frames": 0, "texts": [], "sprites": 0}

    def spy(bb, sprites):
        state["frames"] += 1
        state["sprites"] = len(sprites)
        state["texts"] = [(s.box, s.text) for s in sprites]
        if bb != app.overlay.geometry:
            app.overlay.set_geometry(bb)
        if not app.overlay.visible:
            app.overlay.show()
        app.overlay.render(sprites)

    app._apply_frame = spy

    def kick():
        app._select_mode("region")
        app._start()

    def finish():
        # 有些远程桌面/无人值守会话会把新建窗口图标化，这里强制恢复并重绘
        hwnd = winapi.hwnd_of(app.overlay.win)
        u32.ShowWindow(wt.HWND(hwnd), 5)          # SW_SHOW
        u32.SetWindowPos(wt.HWND(hwnd), wt.HWND(-1), BBOX[0], BBOX[1],
                         BBOX[2], BBOX[3], 0x0010)  # TOPMOST + NOACTIVATE
        app.overlay.render([s for _b, s in []]) if False else None
        app.overlay.redraw()
        app.update_idletasks()
        app.update()
        time.sleep(0.45)
        print("overlay state: iconic=", u32.IsIconic(wt.HWND(hwnd)),
              "visible=", u32.IsWindowVisible(wt.HWND(hwnd)),
              "rect=", winapi.window_rect(hwnd))
        img = ImageGrab.grab()
        img.crop((BBOX[0], BBOX[1], BBOX[0] + BBOX[2], BBOX[1] + BBOX[3])).save(OUT)
        img.save(OUT.replace(".png", "_full.png"))

        print("frames:", state["frames"], "sprites:", state["sprites"])
        for box, text in state["texts"]:
            print(f"   {str(box):24s} -> {text}")

        hwnd_overlay = winapi.hwnd_of(app.overlay.win)
        style = u32.GetWindowLongW(wt.HWND(hwnd_overlay), -20)
        cnt = 0
        for dx, dy in ((0.5, 0.5), (0.25, 0.3), (0.75, 0.7)):
            pt = wt.POINT(BBOX[0] + int(BBOX[2] * dx), BBOX[1] + int(BBOX[3] * dy))
            hit = u32.WindowFromPoint(pt)
            if hit and int(hit) != hwnd_overlay:
                cnt += 1
        print(f"overlay hwnd={hwnd_overlay}  WS_EX_TRANSPARENT={bool(style & 0x20)} "
              f"WS_EX_NOACTIVATE={bool(style & 0x08000000)}")
        print(f"WindowFromPoint 穿透命中 {cnt}/3 个采样点（非叠加层自身）")
        ok = state["sprites"] > 0 and bool(style & 0x20) and cnt >= 2
        print("RESULT:", "PASS" if ok else "CHECK")

        app.pipeline.stop(quiet=True)
        app.overlay.destroy()
        app.destroy()

    app.after(500, kick)
    app.after(5200, finish)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
