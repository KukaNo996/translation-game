"""实时流水线：截图 → OCR → 合并 → 翻译 → 渲染（或内存注入）。

线程模型（三条线程，全部可中断）：
  capture 线程   ：按间隔截图 + OCR + 组帧，把"未译文本"丢进队列；
  translate 线程 ：串行消费队列，批量调用翻译引擎，写回译文；
  主线程(Tk)     ：只做渲染，绝不阻塞。
这样即便用的是网络大模型（单次 1~3 秒），画面刷新也不会卡，
已翻译过的文本从缓存直接出图，新文本下一帧补上。
"""
from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image

from . import capture, ocr as ocr_mod, winapi
from .injector import Injector
from .ocr import TextLine
from .overlay import Sprite
from .translate import TranslationService

Box = Tuple[int, int, int, int]
_KANA = re.compile(r"[\u3040-\u30ff]")


@dataclass
class Status:
    running: bool = False
    mode: str = "region"
    fps: float = 0.0
    ocr_ms: int = 0
    boxes: int = 0
    sprites: int = 0
    pending: int = 0
    engine: str = ""
    note: str = ""
    sample: str = ""          # 最近一条「原文 → 译文」，用来肉眼确认真的在翻
    took_ms: int = 0          # 单帧总耗时


class Pipeline:
    def __init__(self, cfg, service: TranslationService,
                 on_frame: Callable[[Box, List[Sprite]], None],
                 on_status: Callable[[Status], None],
                 log: Callable[[str], None]) -> None:
        self.cfg = cfg
        self.service = service
        self.on_frame = on_frame
        self.on_status = on_status
        self.log = log

        self.mode = "region"
        self.bbox: Box = (0, 0, 0, 0)
        self.hwnd = 0
        self.injector: Optional[Injector] = None
        self.hide_overlay: Optional[Callable] = None   # 抓屏时临时藏起叠加层
        self._cap_hwnd = 0                             # 直接抓这个窗口，不抓屏

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._trans_thread: Optional[threading.Thread] = None
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=64)
        self._inject_q: "queue.Queue[tuple]" = queue.Queue(maxsize=256)
        self._pending: Dict[str, float] = {}
        self._known: Dict[str, str] = {}
        self._failed: Dict[str, float] = {}
        self._engine: Optional[ocr_mod.OcrEngineBase] = None
        self._hasher = capture.FrameHasher()
        self._status = Status()
        self._last_frame: Optional[Tuple[Box, List[TextLine]]] = None
        self._patch_reports: Dict[str, str] = {}
        self._patched: Dict[str, float] = {}      # 已成功写入游戏内存的原文
        self._patch_miss: Dict[str, float] = {}   # 内存里找不到的原文（别每帧重扫）
        self._prev_img: Optional[Image.Image] = None
        self._prev_small = None                   # 上一次的 1/4 灰度缩略（算帧差用）
        self._prev_lines: List[TextLine] = []
        self._last_img: Optional[Image.Image] = None   # 最近一帧原图，供重绘复用
        self._last_full_ocr = 0.0
        self._cjk_frames = 0
        self.lock = threading.RLock()

    # ------------------------------------------------------------ 生命周期
    def start(self, mode: str, bbox: Box, hwnd: int = 0) -> None:
        self.stop(quiet=True)
        self.mode = mode
        self.bbox = bbox
        self.hwnd = hwnd
        self._stop.clear()
        self._pending.clear()
        self._failed.clear()
        self._patched.clear()
        self._patch_miss.clear()
        while not self._inject_q.empty():
            try:
                self._inject_q.get_nowait()
            except queue.Empty:
                break
        self._prev_img = None
        self._prev_small = None
        self._prev_lines = []
        self._last_img = None
        self._cjk_frames = 0
        self._last_full_ocr = 0.0
        self._hasher = capture.FrameHasher()

        if self._engine is None:
            if not ocr_mod.probe():
                raise RuntimeError("没有可用的 OCR 引擎：请确认已安装 "
                                   "rapidocr-onnxruntime，或为系统添加 OCR 语言包。")
            # 注意：引擎必须在采集线程里创建（asyncio 事件循环与 COM 都绑定线程），
            # 所以这里只做可用性探测，真正的初始化放在 _loop() 开头。

        self._cap_hwnd = 0
        if mode == "region":
            h = int(self.cfg.get("region_hwnd", 0) or 0)
            if h and winapi.is_window(h):
                self._cap_hwnd = h
            else:
                self._cap_hwnd = self._window_under(bbox)
        elif mode == "inject":
            self._cap_hwnd = hwnd if hwnd and winapi.is_window(hwnd) else 0

        if mode == "inject":
            pid = int(self.cfg.get("inject", {}).get("pid", 0))
            if not pid:
                raise RuntimeError("请先选择要注入的游戏进程")
            self.injector = Injector()
            self.injector.attach(pid)
            self.log(f"已连接进程 PID={pid}（{winapi.process_name(pid)}）")

        self._thread = threading.Thread(target=self._loop, name="gamelens-capture",
                                        daemon=True)
        self._thread.start()
        self._trans_thread = threading.Thread(target=self._translate_loop,
                                              name="gamelens-translate", daemon=True)
        self._trans_thread.start()
        self._status.running = True
        self._status.mode = mode
        self._status.engine = self._engine.label if self._engine else ""
        self.on_status(self._status)
        self.log(f"开始运行：{self._mode_label(mode)}  区域={bbox}")

    def _window_under(self, bbox: Box) -> int:
        """找出选区正下方那个不属于本工具的顶层窗口，作为抓图对象。"""
        try:
            import ctypes
            import ctypes.wintypes as wt
            u32 = winapi.user32
            cx, cy = bbox[0] + bbox[2] // 2, bbox[1] + bbox[3] // 2
            h = int(u32.WindowFromPoint(wt.POINT(cx, cy)) or 0)
            if not h:
                return 0
            root = int(u32.GetAncestor(wt.HWND(h), winapi.GA_ROOT) or h)
            pid = wt.DWORD()
            u32.GetWindowThreadProcessId(wt.HWND(root), ctypes.byref(pid))
            import os
            if int(pid.value) == os.getpid():
                return 0
            title_len = u32.GetWindowTextLengthW(wt.HWND(root))
            if title_len <= 0:
                return 0            # 桌面/任务栏之类没有标题的窗口跳过
            return root
        except Exception:
            return 0

    def _grab(self, bbox: Box) -> Image.Image:
        if self._cap_hwnd:
            img = capture.grab_window(self._cap_hwnd, bbox)
            if img is not None:
                return img
        if self.hide_overlay is not None:
            return self.hide_overlay(lambda: capture.grab(bbox))
        return capture.grab(bbox)

    def stop(self, quiet: bool = False) -> None:
        self._stop.set()
        for t in (self._thread, self._trans_thread):
            if t and t.is_alive():
                t.join(timeout=2.0)
        self._thread = None
        self._trans_thread = None
        if self.injector:
            n = self.injector.restore_all()
            if n:
                self.log(f"已还原 {n} 处游戏内存原文")
            self.injector.detach()
            self.injector = None
        self._status.running = False
        self.on_status(self._status)
        if not quiet:
            self.log("已停止")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @staticmethod
    def _mode_label(mode: str) -> str:
        return {"region": "屏幕选区汉化", "fullscreen": "全屏汉化",
                "inject": "内部注入式汉化"}.get(mode, mode)

    def reset_cache(self) -> None:
        with self.lock:
            self._known.clear()
            self._failed.clear()

    # ------------------------------------------------------------ 截图/OCR 线程
    def _loop(self) -> None:
        if self._engine is None:
            self._status.note = "正在加载 OCR 模型（首次约 2~5 秒）…"
            self.on_status(self._status)
            try:
                self._engine = ocr_mod.choose_engine(
                    self.cfg.get("ocr_engine", "auto"), self.log,
                    source_lang=str(self.cfg.get("source_lang", "en")))
                self._engine.warmup()      # 首次推理很慢，先热一遍，别算进第一帧
            except Exception as exc:
                self.log(f"OCR 初始化失败：{exc}")
                self._status.note = "OCR 初始化失败"
                self._status.running = False
                self.on_status(self._status)
                return
            self._status.engine = self._engine.label
            self.on_status(self._status)

        interval = max(0.12, float(self.cfg.get("interval_ms", 450)) / 1000.0)
        last = 0.0
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                bbox = self._current_bbox()
                if not bbox:
                    time.sleep(0.35)
                    continue
                self.bbox = bbox
                img = self._grab(bbox)
            except Exception as exc:
                self.log(f"截图失败：{exc}")
                time.sleep(0.6)
                continue

            force = (time.perf_counter() - last) > 2.0
            if not self._hasher.changed(img, force=force):
                self._sleep(interval)
                continue
            last = time.perf_counter()

            try:
                lines = self._ocr_frame(img)
            except Exception as exc:
                self.log(f"OCR 失败：{exc}")
                self._sleep(1.0)
                continue
            ocr_ms = int((time.perf_counter() - t0) * 1000)

            # Windows OCR 偶尔对某些字体彻底失灵：连续几帧都认不出字就换离线模型
            if not lines and getattr(self._engine, "name", "") == "winrt":
                self._empty_frames = getattr(self, "_empty_frames", 0) + 1
                if self._empty_frames >= 4:
                    self.log("Windows OCR 连续 4 帧没认出文字，自动切换到 RapidOCR 离线模型")
                    try:
                        self._engine = ocr_mod.RapidOcr()
                        self._status.engine = self._engine.label
                        self.on_status(self._status)
                    except Exception as exc:
                        self.log(f"切换 OCR 引擎失败：{exc}")
                    self._empty_frames = 0
            else:
                self._empty_frames = 0

            # 另一种失灵：装错语言包的 Windows OCR 会把英文"认"成一堆汉字，
            # 这种结果比认不出更糟（译文直接崩），同样要兜底切离线模型。
            if self._guard_language(lines):
                lines = self._ocr_frame(img, full=True)

            merged = ocr_mod.merge_into_lines(lines)
            groups = ocr_mod.group_paragraphs(merged)
            groups = self._filter(groups)
            groups = sorted(groups, key=lambda g: -g.box[2] * g.box[3])
            groups = groups[: int(self.cfg.get("max_lines", 60))]
            groups.sort(key=lambda g: (g.box[1], g.box[0]))

            self._last_frame = (bbox, groups)
            self._last_img = img
            self._compose(bbox, groups, img, ocr_ms)
            # 只补足"剩余的"间隔：OCR 慢的时候别再多睡一整个 interval
            self._sleep(max(0.02, interval - ((time.perf_counter() - t0) - ocr_ms / 1000)))

    def _guard_language(self, lines: List[TextLine]) -> bool:
        """要求读英文却读出一堆汉字 → 说明引擎语言包不对，切 RapidOCR。"""
        if str(self.cfg.get("source_lang", "en")).lower()[:2] in ("zh", "ja", "ko"):
            return False
        if getattr(self._engine, "name", "") != "winrt" or not lines:
            return False
        cjk_lines = sum(1 for ln in lines if ocr_mod.has_cjk(ln.text, 0.15))
        if cjk_lines < max(2, len(lines) * 0.5):
            self._cjk_frames = 0
            return False
        self._cjk_frames += 1
        if self._cjk_frames < 2:
            return False
        self.log("Windows OCR 把英文认成了汉字（语言包不匹配），切换到 RapidOCR 离线模型")
        try:
            self._engine = ocr_mod.RapidOcr()
            self._engine.warmup()
            self._status.engine = self._engine.label
            self.on_status(self._status)
        except Exception as exc:
            self.log(f"切换 OCR 引擎失败：{exc}")
        self._cjk_frames = 0
        return True

    def _thumb(self, img: Image.Image):
        """1/4 灰度缩略图（int16 数组），帧差比较用；比每帧现算两张图便宜一半。"""
        try:
            import numpy as np
        except Exception:
            return None
        small = img.convert("L").resize((max(1, img.width // 4), max(1, img.height // 4)),
                                        Image.BILINEAR)
        return np.asarray(small, dtype=np.int16)

    def _ocr_frame(self, img: Image.Image, full: bool = False) -> List[TextLine]:
        """只把"这一帧变了的区域"送去 OCR，静止界面几乎不花时间。

        游戏界面大多数时候只有血量数字、一行对白在变，裁出变化区再识别
        通常只要十分之一的时间；缩略图缓存让"没变"这个判断本身也很便宜。
        """
        cached = self._prev_lines
        cur = self._thumb(img)
        since_full = time.time() - self._last_full_ocr

        if not full and cached and cur is not None and self._prev_small is not None \
                and self._prev_small.shape == cur.shape:
            boxes = _diff_bands(self._prev_small, cur, pad=14)
            if boxes is not None and since_full < 8.0:
                changed = sum(b[2] * b[3] for b in boxes)
                if boxes and changed < img.width * img.height * 0.62:
                    fresh: List[TextLine] = []
                    for box in boxes[:6]:        # 太多条就退回整帧，别把一次刷新拖成 6 次推理
                        sub = img.crop((box[0], box[1],
                                        box[0] + box[2], box[1] + box[3]))
                        for ln in self._engine.recognize(sub):
                            x, y, w, h = ln.box
                            ln.box = (x + box[0], y + box[1], w, h)
                            fresh.append(ln)
                    keep = [ln for ln in cached
                            if not any(_hits(ln.box, b) for b in boxes)]
                    self._prev_small = cur
                    self._prev_lines = keep + fresh
                    return self._prev_lines
                if not boxes:                     # 缩略图看不出变化（极细微的抖动）
                    self._prev_small = cur
                    return cached

        lines = self._engine.recognize(img)
        self._last_full_ocr = time.time()
        self._prev_small = cur
        self._prev_lines = lines
        return lines

    def _current_bbox(self) -> Optional[Box]:
        if self.mode == "region":
            # 锁定过窗口时实时跟随窗口位置（拖动/缩放游戏窗口，译文层会跟着走）
            if self._cap_hwnd and winapi.is_window(self._cap_hwnd):
                r = winapi.client_rect_on_screen(self._cap_hwnd)
                if r:
                    return r
                return None
            b = self.cfg.get("region") or [0, 0, 960, 420]
            if len(b) != 4:
                return None
            return int(b[0]), int(b[1]), int(b[2]), int(b[3])
        if self.mode == "fullscreen":
            return winapi.virtual_screen_rect()
        if self.mode == "inject":
            if self.hwnd and winapi.is_window(self.hwnd):
                r = winapi.client_rect_on_screen(self.hwnd)
                if r:
                    return r
            self.cfg.set("_inject_window_lost", True)
            return None
        return None

    def _filter(self, groups: List[TextLine]) -> List[TextLine]:
        min_conf = float(self.cfg.get("min_conf", 0.45))
        min_chars = int(self.cfg.get("min_chars", 2))
        skip_cjk = bool(self.cfg.get("skip_chinese", True))
        src = str(self.cfg.get("source_lang", "en") or "en").lower()[:2]
        out: List[TextLine] = []
        for g in groups:
            text = g.text.strip()
            if not text or g.conf < min_conf:
                continue
            core = [c for c in text if c.isalnum()]
            if len(core) < min_chars:
                continue
            if skip_cjk:
                # 日文里汉字是常态，不能按"汉字多就是中文"来跳过；
                # 只有整段没有假名时才当作已经是中文界面。
                if src == "ja":
                    if ocr_mod.has_cjk(text, 0.3) and not _KANA.search(text):
                        continue
                elif ocr_mod.has_cjk(text, 0.3):
                    continue
            weird = sum(1 for c in text if not (c.isalnum() or c.isspace()
                                                or c in ".,:;!?'\"()[]{}<>+-*/%&@#$…—·，。！？：；、（）《》"))
            if weird > len(text) * 0.25:
                continue
            out.append(g)
        return out

    # ------------------------------------------------------------ 组帧
    def _compose(self, bbox: Box, groups: List[TextLine], img: Image.Image,
                 ocr_ms: int) -> None:
        sprites: List[Sprite] = []
        missing: List[str] = []
        sample = ""

        with self.lock:
            known = dict(self._known)

        for g in groups:
            src = g.text
            if self.mode == "inject" and src in self._patched:
                continue                      # 游戏内部已经显示中文，无需再盖一层
            zh = known.get(src)
            if not zh:
                if src not in self._pending and src not in self._failed:
                    self._enqueue(src)
                missing.append(src)
                continue
            # 译文和原文一样（品牌名、纯数字、词表没收录）就别盖了——
            # 盖上去既不增加信息量，还白白遮住游戏原本更清楚的排版。
            if zh.strip().lower() == src.strip().lower():
                continue
            if not _has_zh(zh) and not _has_zh(src):
                continue
            if self.mode == "inject" and src not in self._patched:
                miss = self._patch_miss.get(src)
                # 内存里找不到的文本，30 秒内不再重扫（否则每帧都要全量搜一次）
                if not (miss and time.time() - miss < 30.0):
                    try:
                        self._inject_q.put_nowait((src, zh))
                    except queue.Full:
                        pass
            if not sample:
                sample = f"{src} → {zh}"
            bg, fg = capture.analyze_colors(img, g.box)
            sprites.append(Sprite(
                box=g.box, text=zh,
                bg=capture.hexc(bg), fg=capture.hexc(fg),
                bold=g.box[3] >= 26,
            ))

        self.on_frame(bbox, sprites)
        now = time.perf_counter()
        dt = now - getattr(self, "_last_compose_at", now)
        self._last_compose_at = now
        st = self._status
        st.ocr_ms = ocr_ms
        st.boxes = len(groups)
        st.sprites = len(sprites)
        st.pending = len(missing)
        st.took_ms = int((time.perf_counter() - now) * 1000) + ocr_ms
        if sample:
            st.sample = sample
        if dt > 0.001:
            inst = 1.0 / dt
            st.fps = round(inst if not st.fps else st.fps * 0.7 + inst * 0.3, 1)
        self.on_status(st)

    def _enqueue(self, text: str) -> None:
        try:
            self._q.put_nowait(text)
            self._pending[text] = time.time()
        except queue.Full:
            pass

    def _sleep(self, seconds: float) -> None:
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            if self._stop.is_set():
                return
            time.sleep(0.02)

    # ------------------------------------------------------------ 翻译线程
    def _translate_loop(self) -> None:
        while not self._stop.is_set():
            self._drain_injection()
            batch: List[str] = []
            try:
                first = self._q.get(timeout=0.3)
                batch.append(first)
            except queue.Empty:
                continue
            deadline = time.time() + 0.05
            while len(batch) < 12 and time.time() < deadline:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            batch = [b for b in dict.fromkeys(batch) if b not in self._known]
            if not batch:
                continue
            t0 = time.perf_counter()
            try:
                results = self.service.translate_batch(batch)
            except Exception as exc:
                self.log(f"翻译异常：{exc}")
                results = [""] * len(batch)
            cost = int((time.perf_counter() - t0) * 1000)
            with self.lock:
                for src, dst in zip(batch, results):
                    self._pending.pop(src, None)
                    if dst:
                        self._known[src] = dst
                    else:
                        self._failed[src] = time.time()
            if any(results):
                self.log(f"翻译 {sum(1 for r in results if r)}/{len(batch)} 条，{cost} ms")

            self._drain_injection()

            # 译文到手，立刻用上一帧的版面重绘一次（复用刚抓的图，不再重复抓屏）
            if self._last_frame:
                bbox, groups = self._last_frame
                img = self._last_img
                if img is None:
                    try:
                        img = self._grab(bbox)
                    except Exception:
                        img = None
                if img is not None:
                    self._compose(bbox, groups, img, self._status.ocr_ms)

    def _drain_injection(self) -> None:
        """把"已知译文但还没写进游戏"的文本补写进去（切换模式后尤其需要）。"""
        if not (self.mode == "inject" and self.injector and self.injector.attached):
            return
        batch: List[tuple] = []
        while len(batch) < 32:
            try:
                batch.append(self._inject_q.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return
        self._apply_injection([b[0] for b in batch], [b[1] for b in batch])

    def _apply_injection(self, batch: Sequence[str], results: Sequence[str]) -> None:
        dry = bool(self.cfg.get("inject", {}).get("dry_run", True))
        for src, zh in zip(batch, results):
            if not zh:
                continue
            try:
                rep = self.injector.patch(src, zh, dry_run=dry)
            except Exception as exc:
                self.log(f"注入失败：{exc}")
                continue
            self._patch_reports[src] = rep.reason
            if rep.patched:
                self._patched[src] = time.time()
                self._patch_miss.pop(src, None)
                self.log(f"注入「{src}」→「{zh}」：{rep.reason}")
            elif not dry:
                if rep.matches:
                    self.log(f"跳过「{src}」：{rep.reason}")
                else:
                    self._patch_miss[src] = time.time()
                    self.log(f"未命中「{src}」：{rep.reason}")


def _has_zh(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _diff_bands(prev, cur, pad: int = 14, threshold: int = 20,
                max_bands: int = 6) -> Optional[List[Box]]:
    """缩略图差分 → 若干"变了的横带"（原图坐标，元素为 (x, y, w, h)）。

    只返回一个大包围盒会逼着整屏重扫（背景一有粒子动画就 45% 以上），
    按横带切开后，对话框更新只重扫对话框那一条，实测能省掉大半推理时间。
    """
    try:
        import numpy as np
    except Exception:
        return None
    try:
        mask = np.abs(cur - prev) > threshold
        if not mask.any():
            return []
        rows = np.where(mask.any(axis=1))[0]
        # 把相邻（间隔 ≤2 格，即 8 像素）的变动行并成一条带
        groups: List[List[int]] = [[int(rows[0])]]
        for r in rows[1:]:
            if r - groups[-1][-1] <= 2:
                groups[-1].append(int(r))
            else:
                groups.append([int(r)])
        h_img, w_img = cur.shape[:2]
        boxes: List[Box] = []
        for grp in groups:
            y0 = max(0, grp[0] * 4 - pad)
            y1 = min(h_img * 4, (grp[-1] + 1) * 4 + pad)
            band = mask[grp[0]:grp[-1] + 1]
            cols = np.where(band.any(axis=0))[0]
            x0 = max(0, int(cols.min()) * 4 - pad)
            x1 = min(w_img * 4, int(cols.max()) * 4 + 4 + pad)
            if x1 - x0 < 8 or y1 - y0 < 8:
                continue
            boxes.append((x0, y0, x1 - x0, y1 - y0))
        return boxes[:max_bands] if len(boxes) <= max_bands else None
    except Exception:
        return None


def _diff_box(a: Image.Image, b: Image.Image, pad: int = 12,
              threshold: int = 20) -> Optional[Box]:
    """返回两张同尺寸图之间"明显变了"的包围盒，没变化返回 None。"""
    try:
        import numpy as np
    except Exception:
        return None
    try:
        ga = np.asarray(a.convert("L").resize((a.width // 4 or 1, a.height // 4 or 1)),
                        dtype=np.int16)
        gb = np.asarray(b.convert("L").resize((b.width // 4 or 1, b.height // 4 or 1)),
                        dtype=np.int16)
        mask = np.abs(ga - gb) > threshold
        if not mask.any():
            return None
        ys, xs = np.where(mask)
        x0 = max(0, int(xs.min()) * 4 - pad)
        y0 = max(0, int(ys.min()) * 4 - pad)
        x1 = min(a.width, int(xs.max()) * 4 + 4 + pad)
        y1 = min(a.height, int(ys.max()) * 4 + 4 + pad)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        return x0, y0, x1 - x0, y1 - y0
    except Exception:
        return None


def _hits(box: Box, region: Box) -> bool:
    """两个矩形是否相交（用于判断缓存文本框是否落在重新识别的区域里）。"""
    x, y, w, h = box
    rx, ry, rw, rh = region
    return not (x + w <= rx or rx + rw <= x or y + h <= ry or ry + rh <= y)
