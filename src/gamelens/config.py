"""配置持久化。"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict


def app_dir() -> Path:
    """配置目录：优先使用 exe 同目录（便携），不可写则落到 %APPDATA%。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parents[2]
    candidate = base / "GameLensData"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_test"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return candidate
    except Exception:
        fallback = Path(os.environ.get("APPDATA", str(Path.home()))) / "GameLens"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


CONFIG_PATH = app_dir() / "config.json"

DEFAULTS: Dict[str, Any] = {
    "mode": "region",                  # region | fullscreen | inject
    "region": [0, 0, 960, 420],
    "region_hwnd": 0,                  # 非 0 时选区锁定到该窗口并实时跟随
    "interval_ms": 450,
    "ocr_engine": "auto",              # auto | winrt | rapid
    "source_lang": "en",
    "skip_chinese": True,
    "min_conf": 0.45,
    "min_chars": 2,
    "max_lines": 60,
    "provider": "litemt",
    "providers": {
        "litemt": {"model": "litemt-0.9", "timeout": 8},
        "ollama": {"base_url": "http://127.0.0.1:11434/v1", "api_key": "ollama",
                   "model": "qwen2.5:7b-instruct", "temperature": 0.2, "timeout": 30},
        "openai": {"base_url": "https://api.openai.com/v1", "api_key": "",
                   "model": "gpt-4o-mini", "temperature": 0.2, "timeout": 30},
        "deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key": "",
                     "model": "deepseek-chat", "temperature": 0.2, "timeout": 30},
        "siliconflow": {"base_url": "https://api.siliconflow.cn/v1", "api_key": "",
                        "model": "Qwen/Qwen2.5-7B-Instruct", "temperature": 0.2, "timeout": 30},
        "google": {"base_url": "https://translate.googleapis.com", "api_key": "", "timeout": 12},
        "deepl": {"base_url": "https://api-free.deepl.com/v2", "api_key": "", "timeout": 15},
        "baidu": {"base_url": "https://fanyi-api.baidu.com/api/trans/vip/translate",
                  "api_key": "", "secret_key": "", "timeout": 15},
    },
    "glossary": {},
    "style": {
        "font_family": "Microsoft YaHei UI",
        "font_scale": 0.62,
        "mask": True,               # 绘制遮底色块以覆盖原文
        "mask_pad": 2,
        "stroke": False,
        "text_color": "auto",
    },
    "inject": {"pid": 0, "hwnd": 0, "dry_run": True, "padding": True},
    "warn_accepted": False,
    "topmost": True,
    "show_source": False,
}


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self) -> None:
        self.data: Dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            if CONFIG_PATH.exists():
                raw = json.loads(CONFIG_PATH.read_text("utf-8"))
                self.data = _deep_merge(DEFAULTS, raw)
        except Exception:
            self.data = copy.deepcopy(DEFAULTS)

    def save(self) -> None:
        try:
            CONFIG_PATH.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    # 便捷访问 -------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def provider_conf(self, name: str) -> Dict[str, Any]:
        return dict(self.data.get("providers", {}).get(name, {}))

    def update_provider(self, name: str, values: Dict[str, Any]) -> None:
        self.data.setdefault("providers", {}).setdefault(name, {}).update(values)
        self.save()
