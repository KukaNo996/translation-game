# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本：产出单文件 GameLens.exe。

用法：python -m PyInstaller build.spec --noconfirm
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs,
                                      collect_submodules)

ROOT = Path(SPECPATH).resolve()
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

# ---- OCR 模型（RapidOCR 自带的 PP-OCR onnx 模型 + 配置）
datas = []
binaries = []
hidden = []
try:
    datas += collect_data_files("rapidocr_onnxruntime")
    binaries += collect_dynamic_libs("rapidocr_onnxruntime")
    # rapidocr 用 importlib 动态加载子包，必须显式声明，否则打包后找不到
    hidden += collect_submodules("rapidocr_onnxruntime")
except Exception:
    pass
try:
    datas += collect_data_files("onnxruntime")
    binaries += collect_dynamic_libs("onnxruntime")
except Exception:
    pass

datas.append((str(ROOT / "assets" / "gamelens.ico"), "assets"))
# 多语种识别模型（日文/英文 PP-OCRv4），打包后 model_dir() 会从 _MEIPASS/assets/models 找
_models = ROOT / "assets" / "models"
if _models.is_dir():
    datas.append((str(_models), "assets/models"))

# ---- Windows OCR（winrt 是命名空间包，显式声明用到的子模块）
WINRT_MODULES = [
    "winrt",
    "winrt.system",
    "_winrt",
    "_winrt_windows_foundation",
    "_winrt_windows_foundation_collections",
    "_winrt_windows_globalization",
    "_winrt_windows_media_ocr",
    "_winrt_windows_graphics_imaging",
    "_winrt_windows_storage_streams",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.globalization",
    "winrt.windows.media.ocr",
    "winrt.windows.graphics.imaging",
    "winrt.windows.storage.streams",
]
for mod in WINRT_MODULES:
    try:
        __import__(mod)
        hidden.append(mod)
    except Exception:
        pass

hidden += [
    "rapidocr_onnxruntime",
    "onnxruntime",
    "onnxruntime.capi",
    "onnxruntime.capi._pybind_state",
    "PIL._tkinter_finder",
    "requests",
    "urllib3",
    "charset_normalizer",
    "idna",
    "certifi",
]

excludes = [
    "matplotlib", "scipy", "pandas", "IPython", "notebook", "pytest",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "torch", "torchvision",
    "tensorflow", "cv2.dnn", "setuptools", "pip", "test", "unittest",
]

a = Analysis(
    [str(SRC / "main.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ---- 砍掉用不到的二进制（体积主要就省在这里）
#  · opencv_videoio_ffmpeg*.dll：OpenCV 的视频解码后端，我们只做截图/OCR，永不调 VideoCapture
#  · PIL\_avif：Pillow 的 AVIF 插件，本项目不读 AVIF
# 这两个加起来压缩前 35MB、压缩后约 15MB —— 砍掉之后 exe 从 110MB 降到 95MB 左右，
# 正好能进 Gitee 100MB 的附件上限，也顺带缩短启动解包时间。
_DROP_BIN = ("opencv_videoio_ffmpeg", "_avif")
_dropped = [b for b in a.binaries if any(k in b[0] for k in _DROP_BIN)]
if _dropped:
    a.binaries = [b for b in a.binaries if b not in _dropped]
    print(f"[build] 已排除 {len(_dropped)} 个无用二进制：")
    for b in _dropped:
        print(f"         - {b[0]}")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GameLens",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "assets" / "gamelens.ico"),
    version=None,
)
