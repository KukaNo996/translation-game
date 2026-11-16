"""OCR 抽象层：Windows 原生 OCR（快）与 RapidOCR（离线、英文更准）双引擎。

两者都输出统一的 TextLine（带像素框），随后做"词→行→段"的合并，
这样送进翻译引擎的才是完整句子，而不是被切碎的词。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from PIL import Image

CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@dataclass
class TextLine:
    box: Tuple[int, int, int, int]   # x, y, w, h（相对截图）
    text: str
    conf: float = 1.0

    @property
    def key(self) -> str:
        x, y, w, h = self.box
        return f"{x // 8}-{y // 8}-{w // 8}-{h // 8}"

    def h(self) -> int:
        return self.box[3]


def has_cjk(text: str, ratio: float = 0.25) -> bool:
    if not text:
        return False
    hits = len(CJK_RE.findall(text))
    return hits / max(1, len(text)) >= ratio


def _union(boxes: Sequence[Tuple[int, int, int, int]]) -> Tuple[int, int, int, int]:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return x0, y0, x1 - x0, y1 - y0


# ------------------------------------------------------------------ 合并算法
def merge_into_lines(items: List[TextLine]) -> List[TextLine]:
    """把零碎词框合并成视觉行。"""
    if not items:
        return []
    items = sorted(items, key=lambda t: (t.box[1] + t.box[3] / 2, t.box[0]))
    rows: List[List[TextLine]] = []
    for it in items:
        cy = it.box[1] + it.box[3] / 2
        placed = False
        for row in rows:
            row_cy = sum(t.box[1] + t.box[3] / 2 for t in row) / len(row)
            row_h = sum(t.box[3] for t in row) / len(row)
            if abs(cy - row_cy) <= max(6, row_h * 0.55):
                row.append(it)
                placed = True
                break
        if not placed:
            rows.append([it])

    out: List[TextLine] = []
    for row in rows:
        row.sort(key=lambda t: t.box[0])
        chunk: List[TextLine] = [row[0]]
        for it in row[1:]:
            prev = chunk[-1]
            gap = it.box[0] - (prev.box[0] + prev.box[2])
            hh = max(8, int((prev.box[3] + it.box[3]) / 2))
            if gap <= hh * 1.15:          # 同一行的相邻词
                chunk.append(it)
            else:
                out.append(_join_chunk(chunk))
                chunk = [it]
        out.append(_join_chunk(chunk))
    out.sort(key=lambda t: (t.box[1], t.box[0]))
    return out


def _join_chunk(chunk: List[TextLine]) -> TextLine:
    text = " ".join(t.text.strip() for t in chunk if t.text.strip())
    text = re.sub(r"\s+", " ", text).strip()
    conf = sum(t.conf for t in chunk) / len(chunk)
    return TextLine(_union([t.box for t in chunk]), text, conf)


def group_paragraphs(lines: List[TextLine], max_lines: int = 4,
                     max_chars: int = 320) -> List[TextLine]:
    """把紧邻的视觉行合并成段（对话框里的多行句子），译文以整段为单位更通顺。"""
    if not lines:
        return []
    lines = sorted(lines, key=lambda t: (t.box[1], t.box[0]))
    used = [False] * len(lines)
    out: List[TextLine] = []

    for i, ln in enumerate(lines):
        if used[i]:
            continue
        group = [ln]
        used[i] = True
        changed = True
        while changed and len(group) < max_lines:
            changed = False
            gx, gy, gw, gh = _union([g.box for g in group])
            for j, cand in enumerate(lines):
                if used[j]:
                    continue
                cx, cy, cw, ch = cand.box
                gap = cy - (gy + gh)
                if gap < -gh * 0.35 or gap > max(4, gh * 0.55):
                    continue
                # 字号差太多的两行（大标题 + 小副标题）不属于同一段
                if abs(ch - gh) > max(3, gh * 0.25):
                    continue
                overlap = min(gx + gw, cx + cw) - max(gx, cx)
                if overlap < min(gw, cw) * 0.45:
                    continue
                if abs(cx - gx) > max(24, gh * 1.6):
                    continue
                if len(" ".join(g.text for g in group + [cand])) > max_chars:
                    continue
                group.append(cand)
                used[j] = True
                changed = True
                break
        text = " ".join(g.text.strip() for g in group)
        text = re.sub(r"\s+", " ", text).strip()
        conf = sum(g.conf for g in group) / len(group)
        out.append(TextLine(_union([g.box for g in group]), text, conf))
    out.sort(key=lambda t: (t.box[1], t.box[0]))
    return out


# ------------------------------------------------------------------ 引擎
class OcrEngineBase:
    name = "base"
    label = "OCR"

    def recognize(self, img: Image.Image) -> List[TextLine]:
        raise NotImplementedError

    def warmup(self) -> None:
        pass

    def close(self) -> None:
        pass


class OcrUnavailable(RuntimeError):
    pass


def model_dir() -> Optional[str]:
    """内置识别模型所在目录（源码运行与 PyInstaller 打包后都能找到）。"""
    import os
    import sys

    roots = []
    if getattr(sys, "_MEIPASS", None):
        roots.append(os.path.join(sys._MEIPASS, "assets", "models"))
    here = os.path.dirname(os.path.abspath(__file__))
    roots.append(os.path.join(here, "..", "..", "assets", "models"))
    roots.append(os.path.join(os.path.dirname(sys.executable), "assets", "models"))
    for r in roots:
        if os.path.isdir(r):
            return os.path.abspath(r)
    return None


# 语言 → (识别模型, 字符表)。模型来自 PP-OCRv4 多语种，体积都在 10MB 以内。
REC_MODELS = {
    "ja": ("japan_PP-OCRv4_rec_mobile.onnx", "japan_dict.txt"),
    "en": ("en_PP-OCRv4_rec_mobile.onnx", "en_dict.txt"),
}


class WinRtOcr(OcrEngineBase):
    """Windows.Media.Ocr —— 系统自带，单帧约 30~80ms，需对应语言包。"""

    name = "winrt"

    PREFERRED = ["en-US", "en-GB", "en", "ja-JP", "ja", "ko-KR", "zh-Hans-CN"]

    def __init__(self, prefer: Optional[str] = None) -> None:
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine
        except Exception as exc:  # pragma: no cover
            raise OcrUnavailable(f"未安装 Windows OCR 运行时：{exc}") from exc

        self._asyncio = __import__("asyncio")
        self._loop = self._asyncio.new_event_loop()

        available = {}
        try:
            for lang in OcrEngine.available_recognizer_languages:
                available[lang.language_tag] = lang
        except Exception:
            pass
        if not available:
            raise OcrUnavailable("系统未安装任何 OCR 语言包（设置 → 时间和语言 → 语言 → 可选功能）")

        order = ([prefer] if prefer else []) + self.PREFERRED
        engine = None
        self.lang = ""
        for tag in order:
            if tag and tag in available:
                engine = OcrEngine.try_create_from_language(available[tag])
                if engine:
                    self.lang = tag
                    break
        if engine is None:
            for tag, lang in available.items():
                engine = OcrEngine.try_create_from_language(lang)
                if engine:
                    self.lang = tag
                    break
        if engine is None:
            raise OcrUnavailable("无法初始化 Windows OCR 引擎")
        self._engine = engine
        self._decoder_mod = None
        self.label = f"Windows OCR ({self.lang})"
        try:
            self._max_dim = int(OcrEngine.max_image_dimension)
        except Exception:
            self._max_dim = 2600

    @staticmethod
    def available_languages() -> List[str]:
        try:
            from winrt.windows.media.ocr import OcrEngine
            return [l.language_tag for l in OcrEngine.available_recognizer_languages]
        except Exception:
            return []

    def recognize(self, img: Image.Image) -> List[TextLine]:
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        scale = 1.0
        w, h = img.size
        biggest = max(w, h)
        if biggest > self._max_dim:
            scale = self._max_dim / biggest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = _encode_png(img)

        async def _run():
            stream = InMemoryRandomAccessStream()
            writer = DataWriter(stream.get_output_stream_at(0))
            writer.write_bytes(buf)
            await writer.store_async()
            await writer.flush_async()
            decoder = await BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            return await self._engine.recognize_async(bitmap)

        result = self._loop.run_until_complete(_run())
        lines: List[TextLine] = []
        inv = 1.0 / scale if scale != 1.0 else 1.0
        for line in result.lines:
            boxes = []
            for word in line.words:
                r = word.bounding_rect
                boxes.append((int(r.x * inv), int(r.y * inv),
                              int(r.width * inv), int(r.height * inv)))
            if not boxes:
                continue
            lines.append(TextLine(_union(boxes), line.text.strip(), 0.9))
        return lines


def _encode_png(img: Image.Image) -> bytes:
    import io
    bio = io.BytesIO()
    img.convert("RGB").save(bio, format="PNG", optimize=False, compress_level=1)
    return bio.getvalue()


class RapidOcr(OcrEngineBase):
    """RapidOCR（PP-OCR ONNX）—— 完全离线自带模型，英文识别更稳。

    实测（1920×1080、9 行文字，i7 笔记本 CPU）：
      · 默认配置           848 ms/帧（其中角度分类器白吃 330 ms）
      · 关掉角度分类器      500 ms/帧
      · det 改为按长边缩到 960  268 ms/帧，识别质量还更好
    游戏界面不可能出现倒着的字，角度分类器纯浪费；det 只负责"框在哪"，
    真正的文字仍从原图裁剪识别，所以缩小检测边长不掉精度。
    """

    name = "rapid"

    # 这些常量在 tools/ 的实测里定下来，别凭感觉改
    DET_SIDE = 960

    def __init__(self, fast: bool = True, lang: str = "ch") -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR as _R
        except Exception as exc:
            raise OcrUnavailable(f"RapidOCR 不可用：{exc}") from exc
        self.lang = (lang or "ch").lower()[:2]
        kwargs = {}
        if fast:
            # 传了 det_*/rec_* 就必须同时给对应的 model_path，否则 update 参数时 KeyError
            kwargs = dict(use_angle_cls=False,
                          det_limit_side_len=self.DET_SIDE, det_limit_type="max",
                          det_model_path=None, rec_batch_num=6, rec_model_path=None)
        rec = REC_MODELS.get(self.lang)
        md = model_dir()
        if rec and md:
            model = os.path.join(md, rec[0])
            keys = os.path.join(md, rec[1])
            if os.path.exists(model) and os.path.exists(keys):
                kwargs.update(rec_model_path=model, rec_keys_path=keys)
        try:
            self._ocr = _R(**kwargs)
        except TypeError:
            self._ocr = _R()          # 老版本不认这些参数，退回默认
        except Exception as exc:
            raise OcrUnavailable(f"RapidOCR 模型加载失败：{exc}") from exc
        self.fast = fast
        tag = {"ja": "日文", "en": "英文"}.get(self.lang, "中英")
        self.label = f"RapidOCR (离线·{tag})"
        self._np = __import__("numpy")

    def warmup(self) -> None:
        """先跑一次假图，把 onnx 的首次推理开销挪出正式流水线（首帧能快 500ms）。"""
        try:
            import numpy as np
            dummy = np.zeros((64, 64, 3), dtype=np.uint8)
            self._ocr(dummy)
        except Exception:
            pass

    def recognize(self, img: Image.Image) -> List[TextLine]:
        import numpy as np
        arr = np.asarray(img.convert("RGB"))[:, :, ::-1]
        arr = np.ascontiguousarray(arr)          # BGR，省掉一次多余拷贝
        result, _elapse = self._ocr(arr)
        lines: List[TextLine] = []
        for item in result or []:
            points, text, score = item[0], item[1], float(item[2])
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            box = (int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys)))
            if not text.strip():
                continue
            lines.append(TextLine(box, text.strip(), score))
        return lines


def choose_engine(preference: str, log=None, source_lang: str = "en") -> OcrEngineBase:
    """按偏好与系统实际情况选择引擎。

    auto 规则（关键：引擎必须"会读这门语言"）：
      · 装了对应语言包（source_lang=en 就要 en / ja / ko 之类拉丁语系包）→ Windows OCR；
      · 只装了中文包、却要读英文界面 → 必须用 RapidOCR。
        实测中文包读英文把 "Memory" 认成 "Memo「y"、"Setting" 认成 "6uas"，
        再喂给翻译引擎就是一堆半英半中的乱码——这是旧版最容易踩的坑。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    pref = {"en": ("en",), "ja": ("ja",), "ko": ("ko",), "ru": ("ru",),
            "zh": ("zh",)}.get((source_lang or "en").lower()[:2], ("en",))

    order: List[str]
    if preference == "winrt":
        order = ["winrt"]
    elif preference == "rapid":
        order = ["rapid"]
    else:
        langs = [t.lower() for t in WinRtOcr.available_languages()]
        usable = any(t.startswith(pref) for t in langs)
        order = ["winrt", "rapid"] if usable else ["rapid", "winrt"]
        if not usable and langs:
            _log(f"系统 OCR 只装了 {', '.join(langs)}，读不了 {source_lang} 文本，改用离线模型")

    errors = []
    for name in order:
        try:
            engine = WinRtOcr(prefer=pref[0] if pref else None) if name == "winrt" \
                else RapidOcr(lang=(source_lang or "en"))
            _log(f"OCR 引擎：{engine.label}")
            return engine
        except OcrUnavailable as exc:
            errors.append(f"{name}: {exc}")
            continue
        except Exception as exc:  # pragma: no cover
            errors.append(f"{name}: {exc}")
            continue
    raise OcrUnavailable("没有可用的 OCR 引擎：\n" + "\n".join(errors))


ENGINE_LABELS = {
    "auto": "自动选择（推荐）",
    "winrt": "Windows 系统 OCR（最快）",
    "rapid": "RapidOCR 离线模型（最稳）",
}


def probe() -> List[str]:
    """快速探测可用引擎（只查依赖与语言包，不加载模型/不创建 asyncio 循环）。"""
    found: List[str] = []
    try:
        langs = WinRtOcr.available_languages()
        if langs:
            found.append("winrt")
    except Exception:
        pass
    try:
        import importlib.util
        if importlib.util.find_spec("rapidocr_onnxruntime") is not None:
            found.append("rapid")
    except Exception:
        pass
    return found
