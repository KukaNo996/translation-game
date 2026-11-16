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
