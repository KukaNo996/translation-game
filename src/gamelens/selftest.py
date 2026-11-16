"""环境自检：不打开界面，检查 OCR / 翻译 / 叠加层 / 注入 四条链路。

用法：
    python src/main.py --selftest
    GameLens.exe --selftest        结果写到 GameLensData/selftest.txt

注意：Windows OCR 的 asyncio 事件循环与 COM 公寓会绑定到创建它的线程，
而 Tk 会在主线程初始化 STA，两者混用会互相卡死。所以 Tk 相关的检查放主线程，
OCR / 翻译 / 注入 放独立工作线程——这也正是程序运行时的真实线程模型。
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import List, Tuple

from .config import app_dir


def _synth_image():
    """生成一张模拟游戏菜单的图片，避免依赖外部素材。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (720, 260), (17, 20, 26))
    d = ImageDraw.Draw(img)
    font = None
    for name in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            font = ImageFont.truetype(name, 26)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    d.text((28, 40), "Press any key to start", font=font, fill=(232, 236, 243))
    d.text((28, 100), "Settings   Inventory   Quit Game", font=font, fill=(232, 236, 243))
    d.text((28, 160), "Level 12 Warrior", font=font, fill=(216, 178, 90))
    return img


class _Runner:
    def __init__(self) -> None:
        self.lines: List[Tuple[str, bool, str]] = []

    def check(self, name: str, fn) -> bool:
        t0 = time.perf_counter()
        try:
            detail = fn() or ""
            self.lines.append((name, True,
                               f"{detail}  ({(time.perf_counter()-t0)*1000:.0f} ms)"))
            print(f"[PASS] {name}  {detail}", flush=True)
            return True
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self.lines.append((name, False, detail))
            print(f"[FAIL] {name}  {detail}", flush=True)
            return False

    # ---------------- 主线程（Tk）检查 ----------------
    def main_checks(self) -> None:
        def _dpi() -> str:
            from . import winapi
            return f"DPI={winapi.system_dpi()}  虚拟桌面={winapi.virtual_screen_rect()}"

        def _tk() -> str:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            root.update()
            root.destroy()
            return f"Tk {tk.TkVersion}"

        def _overlay() -> str:
            import tkinter as tk
            from . import winapi
            from .overlay import OverlayWindow, Sprite
            root = tk.Tk()
            root.withdraw()
            ov = OverlayWindow(root)
            ov.set_geometry((100, 100, 320, 120))
            ov.show()
            ov.render([Sprite(box=(10, 20, 220, 40), text="汉化测试",
                              bg="#101014", fg="#f2f2f2")])
            root.update()
            hwnd = winapi.hwnd_of(ov.win)
            style = winapi.user32.GetWindowLongW(hwnd, -20)
            passthrough = bool(style & 0x20) and bool(style & 0x08000000)
            ov.destroy()
            root.destroy()
            return (f"点击穿透={'是' if passthrough else '否'}  "
                    f"exstyle=0x{style & 0xFFFFFFFF:08x}")

        self.check("Win32 / DPI", _dpi)
        self.check("Tk 界面", _tk)
        self.check("点击穿透叠加层", _overlay)

    # ---------------- 工作线程（OCR / 翻译 / 注入） ----------------
    def worker_checks(self) -> None:
        img = _synth_image()

        def _winrt() -> str:
            from .ocr import WinRtOcr
            langs = WinRtOcr.available_languages()
            eng = WinRtOcr()
            got = eng.recognize(img)
            return f"语言包={langs} 识别={[g.text for g in got][:3]}"

        def _rapid() -> str:
            from .ocr import RapidOcr
            eng = RapidOcr()
            got = eng.recognize(img)
            return f"识别={[g.text for g in got][:3]}"

        def _ocr_pipeline() -> str:
            from .ocr import choose_engine, group_paragraphs, merge_into_lines
            eng = choose_engine("auto")
            merged = merge_into_lines(eng.recognize(img))
            paras = group_paragraphs(merged)
            return f"引擎={eng.label} 行={len(merged)} 段={[p.text[:16] for p in paras]}"

        def _lite() -> str:
            from .translate.lite import LiteMT
            m = LiteMT()
            samples = ["Press START to begin", "Not enough gold",
                       "I don't know what you're talking about."]
            return " / ".join(f"{s}→{m.one(s)}" for s in samples)

        def _japanese() -> str:
            from .translate.ja import JaMT
            m = JaMT()
            samples = ["ゲームを開始する", "セーブデータ", "この扉を開けてください"]
            return " / ".join(f"{s}→{m.one(s)}" for s in samples)

        def _cache() -> str:
            from .config import Config
            from .translate import TranslationService
            svc = TranslationService(Config())
            out = svc.translate_batch(["Quit Game", "Inventory"])
            return f"译文={out} 缓存={svc.cache_size()}"

        def _inject() -> str:
            import ctypes
            import os
            from . import winapi
            from .injector import Injector
            token = "GameLensSelfTest9F3A"
            buf = ctypes.create_unicode_buffer(64)   # 保住缓冲区不被回收
            buf.value = token * 2
            pid = os.getpid()
            inj = Injector()
            inj.proc = winapi.open_process(pid, write=True)
            inj.pid = pid
            try:
                hits = inj.find(token)
                if not hits:
                    raise RuntimeError("未能定位自身进程内的测试文本")
                rep = inj.patch(token, "汉化测试", dry_run=False)
                restored = inj.restore_all()
            finally:
                inj.proc.close()
            return f"命中={len(hits)} 写入={rep.patched} 还原={restored}"

        self.check("Windows 原生 OCR", _winrt)
        self.check("RapidOCR 离线模型", _rapid)
        self.check("OCR 合并流水线", _ocr_pipeline)
        self.check("LiteMT 离线翻译", _lite)
        self.check("日文离线翻译", _japanese)
        self.check("翻译服务与缓存", _cache)
        self.check("进程内存注入", _inject)


def run_selftest() -> int:
    t_start = time.time()
    runner = _Runner()
    runner.main_checks()

    holder: dict = {}

    def worker() -> None:
        try:
            runner.worker_checks()
        except Exception as exc:                 # pragma: no cover
            holder["error"] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()

    th = threading.Thread(target=worker, name="selftest-worker", daemon=True)
    th.start()
    th.join(timeout=180)
    if th.is_alive():
        runner.lines.append(("后台检查", False, "超时（>180s）"))
        print("[FAIL] 后台检查超时", flush=True)
    if holder.get("error"):
        runner.lines.append(("后台检查", False, holder["error"]))

    ok_all = all(ok for _n, ok, _d in runner.lines)
    report = [
        "GameLens 环境自检 " + time.strftime("%Y-%m-%d %H:%M:%S"),
        f"Python {sys.version.split()[0]}   frozen={getattr(sys, 'frozen', False)}",
        "-" * 72,
    ]
    for name, ok, detail in runner.lines:
        report.append(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}")
    report.append("-" * 72)
    report.append(f"结果：{'全部通过' if ok_all else '存在失败项'}   "
                  f"用时 {time.time() - t_start:.1f}s")
    text = "\n".join(report)

    try:
        out = app_dir() / "selftest.txt"
        out.write_text(text, "utf-8")
        print("\n" + text)
        print("\n报告已写入:", out)
    except Exception:
        traceback.print_exc()
    return 0 if ok_all else 1
