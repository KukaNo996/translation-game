"""GameLens 启动入口。

    GameLens.exe                 打开界面
    GameLens.exe --selftest      只做环境自检（结果写到 GameLensData/selftest.txt）
"""
from __future__ import annotations

import sys
import traceback

# 必须在创建任何窗口之前声明 DPI 感知，否则截图坐标和叠加层会对不上
from gamelens import winapi

winapi.enable_dpi_awareness()


def _log_crash(exc_type, exc, tb) -> None:
    try:
        from gamelens.config import app_dir
        path = app_dir() / "crash.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n=== " + __import__("time").strftime("%Y-%m-%d %H:%M:%S") + "\n")
            traceback.print_exception(exc_type, exc, tb, file=fh)
    except Exception:
        pass


def main() -> int:
    sys.excepthook = _log_crash
    if "--selftest" in sys.argv:
        from gamelens.selftest import run_selftest
        try:
            return run_selftest()
        except Exception:
            _log_crash(*sys.exc_info())
            raise
    try:
        from gamelens.app import run
        run()
    except Exception:
        _log_crash(*sys.exc_info())
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
