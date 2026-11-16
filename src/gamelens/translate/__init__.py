"""翻译服务：引擎管理 + 术语表 + 译文缓存 + 离线兜底。"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Dict, List, Optional, Sequence

from ..config import CONFIG_PATH, Config
from .api import PROVIDER_FIELDS, PROVIDER_LABELS, build_engine
from .base import TranslateEngine, TranslationError
from .ja import JaMT
from .lite import LiteMT

CACHE_PATH = CONFIG_PATH.parent / "translation_cache.json"
CACHE_LIMIT = 20000


class TranslationService:
    def __init__(self, cfg: Config, log=None) -> None:
        self.cfg = cfg
        self.log = log or (lambda *_: None)
        self._lock = threading.RLock()
        self._engine: Optional[TranslateEngine] = None
        self._engine_key: str = ""
        self._fallback = LiteMT()
        self._ja = JaMT()
        self._cache: Dict[str, str] = {}
        self._dirty = 0
        self.stats = {"hit": 0, "miss": 0, "error": 0}
        self.last_error: str = ""
        self._load_cache()

    # ------------------------------------------------------------ 缓存
    def _load_cache(self) -> None:
        try:
            if CACHE_PATH.exists():
                raw = json.loads(CACHE_PATH.read_text("utf-8"))
                if isinstance(raw, dict):
                    self._cache = {str(k): str(v) for k, v in raw.items()}
        except Exception:
            self._cache = {}

    def flush_cache(self) -> None:
        with self._lock:
            try:
                if self._dirty:
                    if len(self._cache) > CACHE_LIMIT:
                        for k in list(self._cache)[: len(self._cache) - CACHE_LIMIT]:
                            self._cache.pop(k, None)
                    CACHE_PATH.write_text(
                        json.dumps(self._cache, ensure_ascii=False), "utf-8")
                    self._dirty = 0
            except Exception:
                pass

    def clear_cache(self) -> int:
        with self._lock:
            n = len(self._cache)
            self._cache.clear()
            self._dirty = 1
        self.flush_cache()
        return n

    def cache_size(self) -> int:
        return len(self._cache)

    # ------------------------------------------------------------ 引擎
    def source_lang(self) -> str:
        return str(self.cfg.get("source_lang", "en") or "en").lower()[:2]

    def _provider_key(self) -> str:
        prov = self.cfg.get("provider", "litemt")
        conf = self.cfg.provider_conf(prov)
        return prov + "|" + self.source_lang() + "|" + json.dumps(
            conf, sort_keys=True, ensure_ascii=False)

    def engine(self, force: bool = False) -> TranslateEngine:
        key = self._provider_key()
        with self._lock:
            if force or self._engine is None or key != self._engine_key:
                prov = self.cfg.get("provider", "litemt")
                if prov == "litemt" and self.source_lang() == "ja":
                    # 内置引擎按源语言分流：日文走日语专用词表与句型
                    self._engine = self._ja
                else:
                    self._engine = build_engine(prov, self.cfg.provider_conf(prov))
                self._engine_key = key
                self.log(f"翻译引擎已切换：{self._engine.label}")
            return self._engine

    def invalidate(self) -> None:
        with self._lock:
            self._engine = None
            self._engine_key = ""

    # ------------------------------------------------------------ 术语表
    def _apply_glossary(self, text: str) -> str:
        glossary = self.cfg.get("glossary") or {}
        if not glossary:
            return text
        out = text
        for src, dst in glossary.items():
            if not src or not dst:
                continue
            out = re.sub(re.escape(src), dst, out, flags=re.I)
        return out

    # ------------------------------------------------------------ 翻译
    def _cache_key(self, text: str) -> str:
        prov = self.cfg.get("provider", "litemt")
        return f"{prov}\u0001{self.source_lang()}\u0001{text}"

    def translate_batch(self, texts: Sequence[str]) -> List[str]:
        results: List[str] = [""] * len(texts)
        todo: List[int] = []
        with self._lock:
            for i, t in enumerate(texts):
                key = self._cache_key(t)
                if key in self._cache:
                    results[i] = self._cache[key]
                    self.stats["hit"] += 1
                else:
                    todo.append(i)
        if not todo:
            return results

        items = [texts[i] for i in todo]
        translated: List[str] = []
        try:
            engine = self.engine()
            translated = engine.translate(items, src=self.source_lang())
        except Exception as exc:
            self.stats["error"] += 1
            self.last_error = str(exc)
            self.log(f"翻译失败（{type(exc).__name__}）：{exc}")
            if engine_is_offline(self.cfg):
                translated = [""] * len(items)
            else:
                # 网络/模型不可用时，先用离线引擎顶上，保证界面不空白
                try:
                    translated = self._fallback.translate(items)
                    self.last_error += "（已临时使用离线引擎）"
                except Exception:
                    translated = [""] * len(items)

        if len(translated) != len(items):
            translated = (translated + [""] * len(items))[: len(items)]

        with self._lock:
            for idx, src, dst in zip(todo, items, translated):
                dst = (dst or "").strip()
                if not dst:
                    continue
                dst = self._apply_glossary(dst)
                results[idx] = dst
                self._cache[self._cache_key(src)] = dst
                self._dirty += 1
                self.stats["miss"] += 1
            if self._dirty >= 24:
                self.flush_cache()
        return results

    def test(self) -> str:
        """自检：返回可展示的结果文本（异常以异常抛出）。"""
        engine = self.engine(force=True)
        t0 = time.perf_counter()
        sample = "ゲームを開始" if self.source_lang() == "ja" else "Press any key to start"
        out = engine.translate([sample], src=self.source_lang())
        ms = int((time.perf_counter() - t0) * 1000)
        return f"{engine.label} 正常（{ms} ms）→ {out[0] if out else '(空)'}"


def engine_is_offline(cfg: Config) -> bool:
    return cfg.get("provider", "litemt") in ("litemt",)


__all__ = ["TranslationService", "TranslationError", "PROVIDER_LABELS",
           "PROVIDER_FIELDS", "LiteMT", "engine_is_offline"]
