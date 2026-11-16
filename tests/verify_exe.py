"""验收"打包后的 exe"：不给它开后门，模拟一个普通玩家怎么用。

流程：
  1. 启动 fake_game 假游戏；
  2. 把区域写进 exe 同目录的配置（等价于用户点过一次「锁定游戏窗口」）；
  3. 启动 release/GameLens.exe；
  4. 用真实按键事件按 F8（注册的是全局热键，和玩家在游戏里按一样）；
  5. 等它上屏后截图，检查画面里是否出现中文；
  6. 按 F10 停止，关窗，确认进程真的退干净（不残留、不占用 exe）。

运行：python tests/verify_exe.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EXE = ROOT / "release" / "GameLens.exe"
OUT = ROOT / "tests" / "assets"
RESULT: list[str] = []
OK_ALL = True


def note(ok: bool, msg: str) -> None:
    global OK_ALL
    OK_ALL = OK_ALL and ok
    line = f"[{'PASS' if ok else 'FAIL'}] {msg}"
    RESULT.append(line)
    print(line, flush=True)


def press(vk: int) -> None:
    u = ctypes.windll.user32
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    u.keybd_event(vk, 0, 2, 0)


def main() -> int:
    from PIL import ImageGrab

    from gamelens import winapi

    winapi.enable_dpi_awareness()
    u32 = ctypes.windll.user32
    if not EXE.exists():
        print("找不到 release/GameLens.exe，请先打包")
        return 1

    game = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "fake_game.py"), "--topmost",
         "--pos", "140", "100", "--clicklog", str(OUT / "clicks_exe.txt")],
        stdout=subprocess.PIPE, text=True, encoding="utf-8")
    info = {}
    while True:
        line = game.stdout.readline()
        if not line:
            break
        if line.startswith("FAKEGAME_READY "):
            info = json.loads(line[len("FAKEGAME_READY "):])
            break
    if not info:
        note(False, "假游戏没能启动")
        return 1
    time.sleep(1.0)
    hwnd = int(u32.GetAncestor(wt.HWND(int(info["hwnd"])), 2) or int(info["hwnd"]))
    rect = winapi.client_rect_on_screen(hwnd)
    note(True, f"假游戏就绪 PID={info['pid']} 客户区 {rect}")

    # 预置配置：等价于用户点过「锁定游戏窗口」
    data_dir = EXE.parent / "GameLensData"
    data_dir.mkdir(exist_ok=True)
    cfg = data_dir / "config.json"
    conf = json.loads(cfg.read_text("utf-8")) if cfg.exists() else {}
    conf.update({"mode": "region", "region": list(rect), "region_hwnd": hwnd,
                 "provider": "litemt", "interval_ms": 400, "ocr_engine": "auto"})
    cfg.write_text(json.dumps(conf, ensure_ascii=False, indent=2), "utf-8")

    app = subprocess.Popen([str(EXE)])
    # PyInstaller 单文件版：Popen 的 pid 是引导进程，界面在子进程里，所以按标题找窗口
    wins = []
    t0 = time.time()
    while time.time() - t0 < 25:
        wins = [w for w in winapi.list_windows(min_size=300)
                if "GameLens" in w.title]
        if wins:
            break
        time.sleep(0.5)
    note(bool(wins), f"exe 界面已打开：{wins[0].title if wins else '（没找到窗口）'}"
                     f"，用时 {round(time.time() - t0, 1)}s")
    if not wins:
        app.kill()
        game.kill()
        return 1
    win_pid = wins[0].pid
    # 把主界面挪到游戏区域之外，否则它会把游戏挡住，截图看不到汉化层
    u32.SetWindowPos(wt.HWND(wins[0].hwnd), wt.HWND(-1), 1520, 20, 1180, 880, 0x0010)
    time.sleep(0.8)

    # 基线截图（还没开汉化），后面用它判断"有没有变化"
    u32.SetForegroundWindow(wt.HWND(hwnd))
    time.sleep(0.6)
    ImageGrab.grab(bbox=rect).save(OUT / "exe_baseline.png")
    press(0x77)                      # F8
    time.sleep(9)
    shot = OUT / "exe_region.png"
    ImageGrab.grab(bbox=rect).save(shot)

    # 判断上屏：截图里应当出现明显的中文色块（用与原始画面的差异做粗略判断）
    import numpy as np
    from PIL import Image
    base = Image.open(OUT / "exe_baseline.png").convert("RGB")
    now = Image.open(shot).convert("RGB").resize(base.size)
    diff = np.abs(np.asarray(base, dtype=np.int16) - np.asarray(now, dtype=np.int16))
    changed = float((diff.sum(axis=2) > 60).mean())
    note(changed > 0.01, f"F8 后画面出现汉化层：变化像素 {changed * 100:.1f}%（{shot.name}）")

    press(0x79)                      # F10 停止
    time.sleep(1.5)
    stop_shot = OUT / "exe_stopped.png"
    ImageGrab.grab(bbox=rect).save(stop_shot)
    now2 = Image.open(stop_shot).convert("RGB").resize(base.size)
    d2 = np.abs(np.asarray(base, dtype=np.int16) - np.asarray(now2, dtype=np.int16))
    note(float((d2.sum(axis=2) > 60).mean()) < 0.01, "F10 后汉化层撤掉，画面回到原样")

    # 关窗，确认进程退出（否则会占住 exe，用户下次没法覆盖）
    u32.PostMessageW(wt.HWND(wins[0].hwnd), 0x0010, 0, 0)
    t = time.time()
    while time.time() - t < 8 and app.poll() is None:
        time.sleep(0.2)
    gone = app.poll() is not None or not any(
        w.pid == win_pid and winapi.is_window(w.hwnd) for w in winapi.list_windows(min_size=1)
    )
    note(gone, f"关闭窗口后进程退出（{round(time.time() - t, 1)}s）")
    if not gone:
        app.kill()

    game.kill()
    (OUT / "verify_exe_report.txt").write_text("\n".join(RESULT), "utf-8")
    return 0 if OK_ALL else 2


if __name__ == "__main__":
    sys.exit(main())
