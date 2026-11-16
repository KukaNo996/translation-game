"""依赖自检：CI 打包前先确认环境和仓库内容都对，避免在 PyInstaller 最后一步才崩。

检查项：
  1. Python 版本在 3.10~3.14（3.12 的轮子最全，CI 用它；3.14 本地实测也可用）
  2. rapidocr-onnxruntime 是 1.2.x —— 1.3+ 要求 Python <3.12/<3.13 且 API 不同，
     本项目按 1.2.x 编写；版本不对必须立刻失败，否则打包出来会在运行时报错
  3. 关键模块能 import（rapidocr / cv2 / onnxruntime / PIL / numpy）
  4. 仓库里内置的 OCR 模型存在且大小正常（防止没提交或 LFS 指针化）

用法：python tools/check_deps.py     # 失败返回 1
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Windows 控制台（尤其 GitHub Actions 的 cp1252 环境）默认编码装不下中文，
# 直接 print 就会抛 UnicodeEncodeError。统一改成 UTF-8，别让脚本因为"打印日志"而崩。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

IN_CI = bool(os.environ.get("GITHUB_ACTIONS"))

ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    "japan_PP-OCRv4_rec_mobile.onnx": 8_000_000,
    "en_PP-OCRv4_rec_mobile.onnx": 6_000_000,
    "japan_dict.txt": 10_000,
    "en_dict.txt": 100,
}

fail: list[str] = []


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    # 在 CI 里同时写成 annotation：这样运行页面上直接能看到失败的哪一项，
    # 不用登录下载日志（打包失败排查最耗时的就是这个环节）
    if IN_CI:
        print(f"::error title=依赖自检::{msg}")
    fail.append(msg)


def main() -> int:
    print(f"Python {sys.version.split()[0]} @ {sys.executable}")

    major, minor = sys.version_info[:2]
    if (3, 10) <= (major, minor) <= (3, 14):
        ok(f"Python 版本 {major}.{minor} 在支持范围内")
    else:
        bad(f"Python {major}.{minor} 不在 3.10~3.14 范围内")

    try:
        import importlib.metadata as md
        for pkg in ("rapidocr-onnxruntime", "onnxruntime", "opencv-python",
                    "pyinstaller", "numpy", "pillow"):
            try:
                print(f"         {pkg} = {md.version(pkg)}")
            except Exception:
                print(f"         {pkg} = ?")
        ver = md.version("rapidocr-onnxruntime")
        if ver.startswith("1.2."):
            ok(f"rapidocr-onnxruntime {ver}（本项目按 1.2.x 接口编写）")
        else:
            bad(f"rapidocr-onnxruntime 是 {ver}，需要 1.2.x（1.3+ 的 API 与包结构不同）")
    except Exception as exc:
        bad(f"读不到包版本：{exc}")

    for mod in ("numpy", "PIL", "cv2", "onnxruntime"):
        try:
            __import__(mod)
            ok(f"import {mod}")
        except Exception as exc:
            bad(f"import {mod} 失败：{exc}")

    try:
        from rapidocr_onnxruntime import RapidOCR
        ok("import rapidocr_onnxruntime.RapidOCR")
        try:
            RapidOCR(use_angle_cls=False)      # 真的构造一次，暴露模型路径问题
            ok("RapidOCR 可构造（模型文件可读）")
        except Exception as exc:
            bad(f"RapidOCR 构造失败：{exc}")
    except Exception as exc:
        bad(f"import RapidOCR 失败：{exc}")

    mdir = ROOT / "assets" / "models"
    for name, min_size in MODELS.items():
        p = mdir / name
        if not p.exists():
            bad(f"缺少模型 {name}")
        elif p.stat().st_size < min_size:
            bad(f"模型 {name} 只有 {p.stat().st_size} 字节，可能被 LFS 指针化")
        else:
            ok(f"模型 {name}（{p.stat().st_size / 1048576:.1f} MB）")

    print()
    if IN_CI and os.environ.get("GITHUB_STEP_SUMMARY"):
        try:
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write("### 依赖自检\n\n")
                fh.write("| 项 | 结果 |\n| --- | --- |\n")
                for f in fail or ["全部通过"]:
                    fh.write(f"| {f} | {'❌' if fail else '✅'} |\n")
        except Exception:
            pass
    if fail:
        print(f"依赖自检未通过（{len(fail)} 项）：")
        for f in fail:
            print(f"  - {f}")
        return 1
    print("依赖自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
