"""注入模式验证：在真实进程里定位文本缓冲区、写入译文、再还原。

目标进程用 tools/fake_game.py（界面文字存放在 UTF-16 宽字符缓冲区里）。
运行：python tests/smoke_inject.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

from gamelens import winapi

winapi.enable_dpi_awareness()

from gamelens.injector import Injector  # noqa: E402

CASES = [
    ("Quit Game", "退出游戏"),
    ("Continue", "继续"),
    ("Settings", "设置"),
]


def find_game():
    for w in winapi.list_windows(min_size=1):
        if "Fake Quest" in w.title:
            return w
    return None


def readback(inj: Injector, addr: int, n: int = 40) -> str:
    data = winapi.read_mem(inj.proc, addr, n * 2)
    if not data:
        return ""
    return data.decode("utf-16-le", errors="ignore").split("\x00")[0]


def main() -> int:
    win = find_game()
    if not win:
        print("FAIL: 找不到 Fake Quest 进程，请先运行 tools/fake_game.py")
        return 1
    print(f"目标进程 PID={win.pid} {winapi.process_name(win.pid)}")

    inj = Injector()
    inj.attach(win.pid)
    ok_all = True
    try:
        for src, dst in CASES:
            hits = inj.find(src)
            print(f"\n[{src}] 命中 {len(hits)} 处")
            for h in hits[:3]:
                print(f"   0x{h.addr:x} {h.encoding} 容量={h.capacity}")
            if not hits:
                ok_all = False
                continue
            rep = inj.patch(src, dst, dry_run=False)
            print(f"   写入：{rep.reason} patched={rep.patched} skipped={rep.skipped}")
            now = readback(inj, hits[0].addr)
            print(f"   写后回读：{now!r}")
            if dst not in now:
                ok_all = False

        n = inj.restore_all()
        print(f"\n还原 {n} 处")
        for src, _dst in CASES:
            hits = inj.find(src)
            if hits:
                print(f"   还原后回读 {src!r}: {readback(inj, hits[0].addr)!r}")
    finally:
        inj.detach()

    print("RESULT:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
