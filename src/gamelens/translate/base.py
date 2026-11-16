"""翻译引擎抽象。"""
from __future__ import annotations

from typing import List, Sequence


class TranslationError(RuntimeError):
    pass


class TranslateEngine:
    name = "base"
    label = "翻译引擎"
    offline = False
    supports_batch = True
    max_batch = 16

    def translate(self, texts: Sequence[str], src: str = "en", dst: str = "zh") -> List[str]:
        raise NotImplementedError

    def test(self) -> str:
        """连通性自检，返回一句人话描述。"""
        out = self.translate(["Press any key to start"])
        return f"连通正常，示例译文：{out[0] if out else ''}"

    # 便捷：单条
    def translate_one(self, text: str, src: str = "en", dst: str = "zh") -> str:
        res = self.translate([text], src, dst)
        return res[0] if res else ""
