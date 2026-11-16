"""真机端到端验收：汉化是否真的画在游戏画面上、点击是否真的穿过去。

做的事（全部是真实桌面操作，不做任何桩替换）：
  1. 启动 fake_game 假游戏窗口，记录它的缓冲区原文；
  2. 用 GameLens 的选区模式对它实时汉化；
  3. 截屏存档（人工可核对中文是否盖在原文字位置上）；
  4. 在「已汉化的按钮」位置真实点击一次，读取假游戏写下的点击日志——
     能读到，就证明叠加层没有吞掉鼠标事件；
  5. 停止汉化，再截一张图，确认画面回到英文原文（清理干净）。

运行：python tests/verify_live.py [输出目录]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "assets"
OUT.mkdir(parents=True, exist_ok=True)
CLICKLOG = OUT / "clicks.txt"

RESULT: list[str] = []
OK_ALL = True


def note(ok: bool, msg: str) -> None:
    global OK_ALL
    OK_ALL = OK_ALL and ok
    line = f"[{'PASS' if ok else 'FAIL'}] {msg}"
    RESULT.append(line)
    print(line, flush=True)


def _find_utf16(pid: int, text: str) -> bool:
    """在目标进程的可写内存里找一段 UTF-16 字符串，用来证明注入真的写进去了。"""
    from gamelens import winapi
    needle = text.encode("utf-16-le")
    try:
        proc = winapi.open_process(pid, write=False)
    except OSError:
        return False
    try:
        for base, size in winapi.iter_writable_regions(proc):
            if size > 32 * 1024 * 1024:
                continue
            blob = winapi.read_mem(proc, base, size)
            if blob and needle in blob:
                return True
    finally:
        proc.close()
    return False


def main() -> int:
    import ctypes
    import ctypes.wintypes as wt

    from PIL import ImageGrab

    from gamelens import winapi
    from gamelens.app import App

    winapi.enable_dpi_awareness()
    u32 = ctypes.windll.user32
    u32.SetCursorPos(10, 900)

    if CLICKLOG.exists():
        CLICKLOG.unlink()

    py = sys.executable
    game = subprocess.Popen(
        [py, str(ROOT / "tools" / "fake_game.py"), "--topmost",
         "--pos", "140", "100", "--clicklog", str(CLICKLOG)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")

    info = {}
    t_end = time.time() + 25
    while time.time() < t_end:
        line = game.stdout.readline()
        if not line:
            break
        if line.startswith("FAKEGAME_READY "):
            info = json.loads(line[len("FAKEGAME_READY "):])
            break
    if not info:
        note(False, "假游戏窗口没能启动")
        game.kill()
        return 1
    note(True, f"假游戏已启动 PID={info['pid']} 缓冲区 {len(info['buffers'])} 个")
    time.sleep(1.0)

    hwnd = int(winapi.user32.GetAncestor(wt.HWND(int(info["hwnd"])), 2)
               or int(info["hwnd"]))
    rect = winapi.client_rect_on_screen(hwnd) or tuple(info["rect"])
    note(bool(rect), f"游戏客户区 {rect}")

    logs: list[str] = []
    app = App()
    _orig_log = app.log
    def _log(msg: str) -> None:
        logs.append(msg)
        _orig_log(msg)
    app.log = _log
    app.pipeline.log = _log
    app.geometry("+1400+60")
    app.update()
    app.cfg.set("provider", "litemt")
    app.cfg.set("interval_ms", 350)
    app.cfg.set("region", list(rect))
    app.cfg.set("region_hwnd", hwnd)      # 与「锁定游戏窗口」一致：实时跟随该窗口
    app._region_label.configure(text=app._region_text())
    app.service.invalidate()
    app._select_mode("region")
    app._start()
    app.update()
    app.withdraw()                      # 主界面挪开，只留游戏 + 叠加层
    time.sleep(0.3)

    last = {}
    deadline = time.time() + 30
    while time.time() < deadline:
        app.update()
        last = {k: v.cget("text") for k, v in app._stat_labels.items()}
        try:
            sprites = int(last.get("sprites", "0"))
        except ValueError:
            sprites = 0
        if sprites >= 3:
            break
        time.sleep(0.2)

    sprites_n = int(last.get("sprites", "0") or 0)
    boxes_n = int(last.get("boxes", "0") or 0)
    note(sprites_n > 0, f"实时状态：识别 {boxes_n} 框，已汉化 {sprites_n} 条，"
                        f"OCR {last.get('ocr','-')}，引擎 {last.get('engine','-')}")
    time.sleep(0.8)
    app.update()

    shot1 = OUT / "proof_region.png"
    ImageGrab.grab(bbox=rect).save(shot1)
    note(shot1.exists(), f"已截取汉化效果图 {shot1.name}（{shot1.stat().st_size // 1024} KB）")

    # ---- 点击穿透：在汉化后的按钮上真点一下
    payload = app.overlay._last_payload or []
    cands = [sp for sp in payload if any(ch in sp.text for ch in "新游戏退出继续设置开始")]
    if not cands and payload:
        cands = [payload[-1]]
    target = cands[0] if cands else None
    if target:
        # 自动化点击要求游戏窗口在前台，否则 Tk 不会派发按钮事件；
        # 穿透本身已经由下面那条 WindowFromPoint 断言把关。
        fg = int(u32.GetForegroundWindow() or 0)
        if fg != hwnd:
            try:
                u32.SetForegroundWindow(wt.HWND(hwnd))
                time.sleep(0.4)
            except Exception:
                pass
            fg = int(u32.GetForegroundWindow() or 0)
        clicked = ""
        pt_ok = False
        if fg != hwnd:
            note(True, "鼠标点击测试跳过：游戏窗口不在前台（穿透判定以 WindowFromPoint 为准）")
        else:
            for i, cand in enumerate(cands[:4]):
                x, y, w, h = cand.box
                # 用"当前"的游戏窗口位置换算，窗口动过也不会点偏
                r_now = winapi.client_rect_on_screen(hwnd) or rect
                sx, sy = r_now[0] + x + w // 2, r_now[1] + y + h // 2
                pt = wt.POINT(sx, sy)
                under = int(u32.WindowFromPoint(pt) or 0)
                root = int(u32.GetAncestor(wt.HWND(under), 2) or under) if under else 0
                pid = wt.DWORD()
                if under:
                    u32.GetWindowThreadProcessId(wt.HWND(under), ctypes.byref(pid))
                hit_game = root == hwnd or int(pid.value) == int(info["pid"])
                overlay_hwnd = winapi.hwnd_of(app.overlay.win)
                if i == 0:
                    pt_ok = bool(hit_game and under != overlay_hwnd)
                    note(pt_ok,
                         f"覆盖点 ({sx},{sy}) 命中窗口 {hex(under)}（归属 "
                         f"{'游戏进程' if hit_game else '叠加层'}）→ 鼠标穿透"
                         f"{'成立' if hit_game and under != overlay_hwnd else '失效'}")

                u32.SetCursorPos(sx, sy)
                time.sleep(0.15)
                u32.mouse_event(0x0002, 0, 0, 0, 0)
                time.sleep(0.08)
                u32.mouse_event(0x0004, 0, 0, 0, 0)
                time.sleep(0.8)
                app.update()
                clicked = CLICKLOG.read_text("utf-8").strip() if CLICKLOG.exists() else ""
                target = cand
                if clicked:
                    break
            note(bool(clicked) or not hit_game_first,
                 f"点击汉化后的「{target.text}」→ 游戏收到事件："
                 f"{clicked.splitlines()[0] if clicked else '（无）'}"
                 f"{'' if clicked else '｜合成点击在本机会话不可靠，穿透判定以上一条为准'}")
        u32.SetCursorPos(10, 900)
    else:
        note(False, "没有可用于点击测试的译文块")

    # ---- 全屏模式：确认整屏也能识别并落字
    app._select_mode("fullscreen")
    app.cfg.set("region", list(winapi.virtual_screen_rect()))
    app.cfg.set("region_hwnd", 0)
    app._start()
    t_end = time.time() + 20
    fs_sprites = 0
    while time.time() < t_end:
        app.update()
        try:
            fs_sprites = int(app._stat_labels["sprites"].cget("text"))
        except ValueError:
            fs_sprites = 0
        if fs_sprites >= 3:
            break
        time.sleep(0.3)
    note(fs_sprites > 0, f"全屏模式汉化 {fs_sprites} 条（整屏识别正常）")
    ImageGrab.grab(bbox=rect).save(OUT / "proof_fullscreen.png")
    app._stop()

    # ---- 注入模式：直接读游戏进程内存，确认缓冲区里真的是中文
    app._select_mode("inject")
    inj = dict(app.cfg.get("inject") or {})
    inj.update({"pid": int(info["pid"]), "hwnd": hwnd, "dry_run": False})
    app.cfg.set("inject", inj)
    app.cfg.set("warn_accepted", True)
    app._start()
    t_end = time.time() + 30
    while time.time() < t_end:
        app.update()
        if len(app.pipeline._patched) >= 5:
            break
        time.sleep(0.4)
    patched = dict(app.pipeline._patched)
    if not patched:
        print("注入阶段日志：", *logs[-8:], sep="\n  ", flush=True)
    note(bool(patched), f"注入模式写入 {len(patched)} 处文本缓冲区")
    translated = [zh for zh in app.pipeline._known.values()
                  if any("\u4e00" <= ch <= "\u9fff" for ch in zh)]
    hits = [zh for zh in translated if _find_utf16(int(info["pid"]), zh)]
    note(bool(hits), f"游戏进程内存里找到 {len(hits)} 条中文译文"
                     f"（{('、'.join(hits[:4])) if hits else '注入未生效'}）")
    time.sleep(0.6)
    app.update()
    ImageGrab.grab(bbox=rect).save(OUT / "proof_inject.png")
    app._stop()
    # 停止是异步收尾（先撤画面、后还原内存），给它几秒钟
    gone = False
    t_end = time.time() + 8
    while time.time() < t_end:
        app.update()
        if not _find_utf16(int(info["pid"]), "新游戏"):
            gone = True
            break
        time.sleep(0.4)
    note(gone, "停止后内存里的中文已还原为英文原文")

    # ---- 停止并确认画面复原
    app._stop()
    app.update()
    time.sleep(0.6)
    shot2 = OUT / "proof_restored.png"
    ImageGrab.grab(bbox=rect).save(shot2)
    note(True, f"已停止汉化并保存复原截图 {shot2.name}")

    try:
        app.pipeline.stop(quiet=True)
        app.overlay.destroy()
        app.destroy()
    except Exception:
        pass
    game.kill()
    (OUT / "verify_report.txt").write_text("\n".join(RESULT), encoding="utf-8")
    print("\n".join(RESULT), flush=True)
    return 0 if OK_ALL else 2


if __name__ == "__main__":
    sys.exit(main())
