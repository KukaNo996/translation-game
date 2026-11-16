"""外部翻译：OpenAI 兼容大模型 / Google / DeepL / 百度。

OpenAI 兼容协议一份代码通吃 DeepSeek、Moonshot、通义、硅基流动、
Ollama、LM Studio、vLLM、OpenAI 官方等，只改 base_url + model 即可。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
from typing import Any, Dict, List, Sequence

import requests

from .base import TranslationError, TranslateEngine

SYSTEM_PROMPT = (
    "你是资深游戏本地化译者。用户会给你一个 JSON 字符串数组，每条是一条游戏界面文本"
    "（按钮、菜单、对话框、提示、物品名等）。请把它们逐条译成简体中文。"
    "要求：\n"
    "1) 只输出一个 JSON 字符串数组，元素个数必须与输入完全一致，顺序对应；\n"
    "2) 译文简洁地道，符合中文游戏用语习惯，按钮类尽量不超过 8 个字；\n"
    "3) 严格保留 {0} {name} %s %d [[KEY]] 等占位符、数字、以及 F1/Ctrl/Shift 等按键名；\n"
    "4) 已经是中文的条目原样返回；\n"
    "5) 不要输出解释、不要 markdown 代码块。"
)


def _strip_fence(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


class OpenAICompat(TranslateEngine):
    """所有 /chat/completions 协议的网关。"""

    def __init__(self, name: str, label: str, base_url: str, api_key: str,
                 model: str, temperature: float = 0.2, timeout: int = 30) -> None:
        self.name = name
        self.label = label
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = model or "gpt-4o-mini"
        self.temperature = temperature
        self.timeout = timeout
        self.supports_batch = True
        self.max_batch = 16
        self._session = requests.Session()

    # ------------------------------------------------------------------
    def _post(self, texts: List[str], src: str, dst: str) -> List[str]:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: Dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
            ],
        }
        try:
            resp = self._session.post(url, headers=headers, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise TranslationError(f"连接失败：{exc}") from exc
        if resp.status_code >= 400:
            snippet = resp.text[:300].replace("\n", " ")
            raise TranslationError(f"HTTP {resp.status_code}：{snippet}")
        try:
            data = resp.json()
        except Exception as exc:
            raise TranslationError(f"响应不是 JSON：{resp.text[:200]}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationError(f"响应结构异常：{str(data)[:200]}") from exc
        return self._parse(content, len(texts))

    @staticmethod
    def _parse(content: str, expect: int) -> List[str]:
        text = _strip_fence(content)
        try:
            arr = json.loads(text)
            if isinstance(arr, list):
                out = [str(x) for x in arr]
                if len(out) == expect:
                    return out
        except Exception:
            pass
        # 兜底：按行解析（可能带 1. 2. 编号）
        lines = [re.sub(r"^\s*(?:\d+[.、)]|[-*])\s*", "", ln).strip()
                 for ln in text.splitlines() if ln.strip()]
        lines = [ln.strip('"').strip() for ln in lines]
        if len(lines) == expect:
            return lines
        raise TranslationError(f"返回条目数不匹配（期望 {expect}，实际 {len(lines)}）")

    def translate(self, texts: Sequence[str], src: str = "en", dst: str = "zh") -> List[str]:
        items = list(texts)
        if not items:
            return []
        out: List[str] = []
        batch = self.max_batch
        i = 0
        while i < len(items):
            chunk = items[i:i + batch]
            try:
                out.extend(self._post(chunk, src, dst))
            except TranslationError:
                if len(chunk) > 1:
                    batch = max(1, len(chunk) // 2)
                    continue
                raise
            i += len(chunk)
        return out


class GoogleFree(TranslateEngine):
    """Google 网页翻译端点，免密钥，适合快速验证；高频会被限流。"""

    name = "google"
    supports_batch = False

    def __init__(self, base_url: str = "https://translate.googleapis.com",
                 timeout: int = 12) -> None:
        self.base_url = (base_url or "https://translate.googleapis.com").rstrip("/")
        self.timeout = timeout
        self.label = "Google 翻译（免费端点）"
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0 GameLens/1.0"})

    def translate(self, texts: Sequence[str], src: str = "en", dst: str = "zh") -> List[str]:
        out = []
        for text in texts:
            params = {"client": "gtx", "sl": src or "auto", "tl": "zh-CN", "dt": "t", "q": text}
            try:
                r = self._session.get(f"{self.base_url}/translate_a/single", params=params,
                                      timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                segs = data[0] or []
                out.append("".join(s[0] for s in segs if s and s[0]).strip() or text)
            except Exception as exc:
                raise TranslationError(f"Google 翻译失败：{exc}") from exc
        return out


class DeepL(TranslateEngine):
    name = "deepl"

    def __init__(self, base_url: str, api_key: str, timeout: int = 15) -> None:
        self.base_url = (base_url or "https://api-free.deepl.com/v2").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.label = "DeepL"
        self.supports_batch = True
        self.max_batch = 25

    def translate(self, texts: Sequence[str], src: str = "en", dst: str = "zh") -> List[str]:
        if not self.api_key:
            raise TranslationError("未填写 DeepL API Key")
        payload = {"text": list(texts), "target_lang": "ZH"}
        if src and src != "auto":
            payload["source_lang"] = src.upper()
        try:
            r = requests.post(f"{self.base_url}/translate", data=payload,
                              headers={"Authorization": f"DeepL-Auth-Key {self.api_key}"},
                              timeout=self.timeout)
        except requests.RequestException as exc:
            raise TranslationError(f"连接失败：{exc}") from exc
        if r.status_code >= 400:
            raise TranslationError(f"HTTP {r.status_code}：{r.text[:200]}")
        data = r.json()
        return [t.get("text", "") for t in data.get("translations", [])]


class Baidu(TranslateEngine):
    name = "baidu"

    def __init__(self, url: str, appid: str, secret: str, timeout: int = 15) -> None:
        self.url = url or "https://fanyi-api.baidu.com/api/trans/vip/translate"
        self.appid = appid
        self.secret = secret
        self.timeout = timeout
        self.label = "百度翻译开放平台"

    def translate(self, texts: Sequence[str], src: str = "en", dst: str = "zh") -> List[str]:
        if not (self.appid and self.secret):
            raise TranslationError("未填写百度 appid / 密钥")
        out: List[str] = []
        # 百度支持用 \n 一次翻多条
        joined = "\n".join(texts)
        salt = str(random.randint(32768, 65536))
        sign = hashlib.md5((self.appid + joined + salt + self.secret).encode("utf-8")).hexdigest()
        params = {"q": joined, "from": src if src != "auto" else "auto", "to": dst,
                  "appid": self.appid, "salt": salt, "sign": sign}
        try:
            r = requests.get(self.url, params=params, timeout=self.timeout)
            data = r.json()
        except Exception as exc:
            raise TranslationError(f"请求失败：{exc}") from exc
        if "error_code" in data and str(data["error_code"]) != "52000":
            raise TranslationError(f"百度接口错误 {data.get('error_code')}：{data.get('error_msg')}")
        for item in data.get("trans_result", []):
            out.append(item.get("dst", ""))
        if len(out) != len(texts):
            raise TranslationError("百度返回条目数不匹配")
        return out


def build_engine(provider: str, conf: Dict[str, Any]) -> TranslateEngine:
    """按配置实例化翻译引擎。"""
    provider = (provider or "litemt").lower()
    if provider == "litemt":
        from .lite import LiteMT
        return LiteMT()
    if provider in ("openai", "deepseek", "ollama", "siliconflow", "custom"):
        labels = {
            "openai": "OpenAI", "deepseek": "DeepSeek", "ollama": "Ollama 本地大模型",
            "siliconflow": "硅基流动", "custom": "自定义 OpenAI 兼容 API",
        }
        return OpenAICompat(
            name=provider, label=labels.get(provider, provider),
            base_url=conf.get("base_url", ""), api_key=conf.get("api_key", ""),
            model=conf.get("model", ""), temperature=float(conf.get("temperature", 0.2)),
            timeout=int(conf.get("timeout", 30)),
        )
    if provider == "google":
        return GoogleFree(conf.get("base_url", ""), timeout=int(conf.get("timeout", 12)))
    if provider == "deepl":
        return DeepL(conf.get("base_url", ""), conf.get("api_key", ""),
                     timeout=int(conf.get("timeout", 15)))
    if provider == "baidu":
        return Baidu(conf.get("base_url", ""), conf.get("api_key", ""),
                     conf.get("secret_key", ""), timeout=int(conf.get("timeout", 15)))
    raise TranslationError(f"未知翻译引擎：{provider}")


PROVIDER_LABELS = {
    "litemt": "LiteMT 内置离线引擎（无需网络）",
    "ollama": "Ollama 本地大模型（局域网/本机）",
    "deepseek": "DeepSeek（OpenAI 兼容）",
    "openai": "OpenAI（OpenAI 兼容）",
    "siliconflow": "硅基流动 SiliconFlow",
    "custom": "自定义 OpenAI 兼容 API",
    "google": "Google 翻译（免费，免密钥）",
    "deepl": "DeepL API",
    "baidu": "百度翻译开放平台",
}

PROVIDER_FIELDS = {
    "litemt": [],
    "ollama": ["base_url", "model", "timeout"],
    "openai": ["base_url", "api_key", "model", "temperature", "timeout"],
    "deepseek": ["base_url", "api_key", "model", "temperature", "timeout"],
    "siliconflow": ["base_url", "api_key", "model", "temperature", "timeout"],
    "custom": ["base_url", "api_key", "model", "temperature", "timeout"],
    "google": ["base_url", "timeout"],
    "deepl": ["base_url", "api_key", "timeout"],
    "baidu": ["base_url", "api_key", "secret_key", "timeout"],
}
