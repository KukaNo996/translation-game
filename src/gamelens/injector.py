"""内部注入式汉化：在目标游戏进程里定位文本缓冲区，直接把译文写回去。

这是"真的替换游戏内部文本"的做法，不是盖一层贴图：
  1. 用 VirtualQueryEx 遍历目标进程所有可写已提交内存；
  2. 按 UTF-16LE / UTF-8 / GBK 三种编码搜索屏幕上的原文字节；
  3. 命中后读缓冲区容量（找 UTF-16 的双字节结束符），
     把译文写回去，不足部分补空格（绝不越界写）；
  4. 全程记录 (地址, 原始字节)，停止汉化时一键还原。

性能上有三个关键设计（旧版每条文本都要把 1.2GB 可写内存按三种编码各扫一遍，
一条对话就能把流水线卡住好几秒）：

  · **一遍扫完三种编码**：同一个内存块只读一次，三条 needle 在这块里一起 find，
    省掉 2/3 的读取量；
  · **内存区快照 + 命中区优先**：VirtualQueryEx 的结果缓存 5 秒，且上次命中过的
    内存区排到最前面——游戏字符串几乎总在同一个堆里，第二次搜索通常几毫秒就中；
  · **扫描预算**：字节预算 + 时间预算双上限，找不到就老实返回"未找到"，
    绝不为了找一条文本把主循环拖死。

注意：这一步会写游戏内存，天然有风险。因此默认是"演练模式"（只找不写），
必须在界面上手动关闭演练后才会真正写入；退出时会自动还原所有已写入位置。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import winapi

CHUNK = 3 * 1024 * 1024
OVERLAP = 16
ENCODINGS = ("utf-16-le", "utf-8", "gbk")

REGION_TTL = 5.0        # 内存区快照有效期（秒）
MATCH_TTL = 20.0        # 命中地址缓存有效期（秒）
CAP_RATIO = 3           # 缓冲区容量上限 = 原文长度 × 3（防止把隔壁字符串一起吃掉）


@dataclass
class Match:
    addr: int
    encoding: str
    capacity: int      # 可写入的字符数（不含结束符）
    source: str


@dataclass
class Patch:
    addr: int
    original: bytes
    written: bytes
    encoding: str


@dataclass
class PatchReport:
    text: str
    translation: str
    matches: int = 0
    patched: int = 0
    skipped: int = 0
    reason: str = ""
    scan_ms: int = 0
    scan_mb: float = 0.0


class Injector:
    def __init__(self) -> None:
        self.proc: Optional[winapi.ProcHandle] = None
        self.pid = 0
        self._lock = threading.RLock()
        self._patches: List[Patch] = []
        self._match_cache: Dict[str, Tuple[float, List[Match]]] = {}
        self._regions: List[Tuple[int, int]] = []
        self._regions_at = 0.0
        self._hot: Dict[int, int] = {}          # 出现过的命中区 → 命中次数
        self.scanned_bytes = 0
        self.max_scan_bytes = 256 * 1024 * 1024  # 单次搜索的字节上限
        self.max_scan_ms = 500                   # 单次搜索的时间上限
        self.last_scan_ms = 0
        self.last_scan_bytes = 0

    # ------------------------------------------------------------ 连接
    def attach(self, pid: int) -> None:
        self.detach()
        self.proc = winapi.open_process(pid, write=True)
        self.pid = pid
        self._patches.clear()
        self._match_cache.clear()
        self._regions.clear()
        self._hot.clear()
        self._regions_at = 0.0

    def detach(self) -> None:
        self.restore_all()
        if self.proc:
            self.proc.close()
        self.proc = None
        self.pid = 0
        self._regions.clear()
        self._hot.clear()

    @property
    def attached(self) -> bool:
        return self.proc is not None and self.proc.handle != 0

    # ------------------------------------------------------------ 内存区
    def regions(self) -> List[Tuple[int, int]]:
        """可写内存区快照；命中过的区排最前（字符串几乎总在同一片堆里）。"""
        now = time.perf_counter()
        if not self._regions or now - self._regions_at > REGION_TTL:
            self._regions = list(winapi.iter_writable_regions(self.proc))
            self._regions_at = now
        if not self._hot:
            return list(self._regions)
        hot = self._hot
        return sorted(self._regions, key=lambda r: -hot.get(r[0], 0))

    # ------------------------------------------------------------ 搜索
    @staticmethod
    def _needles(text: str) -> List[Tuple[str, bytes]]:
        out: List[Tuple[str, bytes]] = []
        for enc in ENCODINGS:
            try:
                out.append((enc, text.encode(enc)))
            except UnicodeEncodeError:
                continue
        return out

    def find(self, text: str, limit: int = 12) -> List[Match]:
        """在目标进程里查找这段文本的所有可写副本（一遍扫完三种编码）。"""
        if not self.attached:
            raise RuntimeError("未连接到游戏进程")
        text = text.strip()
        if len(text) < 2:
            return []
        now = time.perf_counter()
        with self._lock:
            hit = self._match_cache.get(text)
            if hit and now - hit[0] < MATCH_TTL:
                return hit[1]

        needles = self._needles(text)
        if not needles:
            return []

        # 每个编码单独限额：UTF-16 是 Windows 游戏的主力（宽字符缓冲区），
        # 单字节编码的副本经常是源码/常量里的 ASCII 串，抢满名额会让真正的
        # 缓冲区反而搜不到——所以 utf-8/gbk 只留少量名额，最后按优先级排序。
        caps = {enc: (limit if enc == "utf-16-le" else max(3, limit // 3))
                for enc, _ in needles}
        found: Dict[str, List[Match]] = {enc: [] for enc, _ in needles}

        t0 = time.perf_counter()
        scanned = 0
        deadline = t0 + self.max_scan_ms / 1000.0
        for base, size in self.regions():
            if scanned >= self.max_scan_bytes or time.perf_counter() > deadline:
                break
            if len(found.get("utf-16-le", [])) >= limit:
                break
            offset = 0
            while offset < size:
                if scanned >= self.max_scan_bytes or time.perf_counter() > deadline:
                    break
                read_len = min(CHUNK, size - offset)
                if read_len <= 0:
                    break
                data = winapi.read_mem(self.proc, base + offset, read_len)
                scanned += read_len
                if not data:
                    break
                for enc, needle in needles:
                    bucket = found[enc]
                    if len(bucket) >= caps[enc]:
                        continue
                    pos = 0
                    while len(bucket) < caps[enc]:
                        idx = data.find(needle, pos)
                        if idx < 0:
                            break
                        end = idx + len(needle)
                        # 单字节编码只认"紧接着 NUL 收尾"的 C 字符串。
                        # 否则会命中源码/常量块里的英文片段（后面跟的是引号或逗号），
                        # 往那种地方写中文等于破坏程序自己的数据，也让还原变脏。
                        if enc != "utf-16-le" and end < len(data) and data[end] != 0:
                            pos = end
                            continue
                        addr = base + offset + idx
                        bucket.append(Match(addr, enc,
                                            self._capacity(addr, len(text), enc), text))
                        pos = end
                offset += max(1, read_len - OVERLAP)

        # 按编码优先级合并，同一地址只保留一条（ASCII 的 utf-8/gbk needle 完全相同，
        # 不去重会把同一处写两遍，还原链也会变乱）
        out: List[Match] = []
        seen = set()
        for enc, _ in needles:
            for m in found[enc]:
                if m.addr in seen:
                    continue
                seen.add(m.addr)
                out.append(m)
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break

        self.last_scan_ms = int((time.perf_counter() - t0) * 1000)
        self.last_scan_bytes = scanned
        self.scanned_bytes = scanned
        if out:
            # 记下命中的区，下次优先扫
            for m in out:
                for base, size in self._regions:
                    if base <= m.addr < base + size:
                        self._hot[base] = self._hot.get(base, 0) + 1
                        break
        with self._lock:
            if len(self._match_cache) > 500:      # 长期运行别让缓存无限长
                self._match_cache.clear()
            self._match_cache[text] = (time.perf_counter(), out)
        return out

    def _capacity(self, addr: int, src_len: int, enc: str) -> int:
        """估算缓冲区能放多少字符。

        上限刻意压到 原文长度×3：字符串常量常常紧挨着别的常量，
        一旦把隔壁的 0 当成自己的结束符就会越界改写别人的数据。
        """
        hard_cap = max(src_len + 8, src_len * CAP_RATIO)
        if enc != "utf-16-le":
            probe = winapi.read_mem(self.proc, addr, min(1024, max(64, src_len * 4 + 32)))
            if not probe:
                return src_len
            end = probe.find(b"\x00")
            if end < 0:
                return src_len
            return max(src_len, min(end, hard_cap))
        probe = winapi.read_mem(self.proc, addr, min(2048, src_len * 2 + 128))
        if not probe:
            return src_len
        for i in range(0, len(probe) - 1, 2):
            if probe[i] == 0 and probe[i + 1] == 0:
                found = i // 2
                return max(src_len, min(found, hard_cap))
        return src_len

    # ------------------------------------------------------------ 写入
    def patch(self, text: str, translation: str, dry_run: bool = True,
              max_targets: int = 6) -> PatchReport:
        report = PatchReport(text=text, translation=translation)
        if not translation.strip():
            report.reason = "译文为空"
            return report
        t0 = time.perf_counter()
        matches = self.find(text)
        report.scan_ms = int((time.perf_counter() - t0) * 1000)
        report.scan_mb = round(self.last_scan_bytes / (1024 * 1024), 1)
        report.matches = len(matches)
        if not matches:
            report.reason = (f"内存中未找到该文本（扫了 {report.scan_mb} MB / "
                             f"{report.scan_ms} ms；可能是 GPU 纹理文字或已加密）")
            return report
        if dry_run:
            report.reason = (f"演练模式：找到 {len(matches)} 处可写副本，未写入"
                             f"（{report.scan_ms} ms）")
            return report

        dead: List[int] = []
        for m in matches[:max_targets]:
            if m.encoding == "utf-16-le":
                body = translation.encode("utf-16-le")
                room = m.capacity * 2
                if len(body) > room:
                    report.skipped += 1
                    continue
                data = body + b"\x00\x00" * ((room - len(body)) // 2)
            else:
                try:
                    body = translation.encode(m.encoding, errors="ignore")
                except Exception:
                    report.skipped += 1
                    continue
                # 单字节/UTF-8 缓冲区的"容量"不可靠（往后找 0 可能跨到别的变量上），
                # 所以严格按原文字节数写：翻得比原文长就跳过，绝不越界。
                room = len(text.encode(m.encoding, errors="ignore"))
                if len(body) > room:
                    report.skipped += 1
                    continue
                data = body + b" " * (room - len(body))

            original = winapi.read_mem(self.proc, m.addr, len(data))
            if original is None:
                report.skipped += 1
                dead.append(m.addr)
                continue
            if winapi.write_mem(self.proc, m.addr, data):
                with self._lock:
                    self._patches.append(Patch(m.addr, original, data, m.encoding))
                report.patched += 1
            else:
                report.skipped += 1
                dead.append(m.addr)

        # 地址失效（内存被游戏回收/搬家）就把这条缓存丢掉，下次重新搜
        if dead:
            with self._lock:
                self._match_cache.pop(text, None)

        if report.patched:
            report.reason = f"已写入 {report.patched} 处（{report.scan_ms} ms）"
        elif report.skipped:
            report.reason = "缓冲区放不下译文（长度不足），已跳过"
        return report

    def restore_all(self) -> int:
        with self._lock:
            patches = list(self._patches)
            self._patches.clear()
        n = 0
        if not self.attached:
            return 0
        for p in reversed(patches):
            if winapi.write_mem(self.proc, p.addr, p.original):
                n += 1
        return n

    def patch_count(self) -> int:
        return len(self._patches)
