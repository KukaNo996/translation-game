"""翻译覆盖率基准：拿真实游戏的文本语料量离线引擎的"汉化到什么程度"。

指标：
  完全汉化  —— 译文里没有残留英文实词，且中文占比 ≥ 0.75（玩家能直接读）
  部分汉化  —— 有中文但夹着英文
  未汉化    —— 基本没翻出来
  残留词    —— 输出里剩下的英文单词（按出现次数排序，用来指导补词表）
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from gamelens.translate.lite import LiteMT  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
REPORT = os.path.join(ROOT, "GameLensData", "bench_translate.json")

# ---- 只保留"玩家真会看到的那类文本" ----
META = re.compile(r"^(Sequence|Display Name|Menu Text|Dialogue Text|Actor|Conversant|"
                  r"Parenthetical|Audio Files|Video File|Response Menu|Portraits|"
                  r"Voice Actor|Event Guid|Title|Description|Pictures|Entry)\b")
FUNC = {"the", "a", "an", "to", "of", "and", "is", "are", "was", "were", "i", "you",
        "my", "your", "it", "that", "this", "do", "does", "not", "for", "on", "in",
        "with", "we", "he", "she", "they", "me", "at", "so", "if", "but", "be", "can",
        "will", "have", "has", "just", "what", "how", "no", "yes", "oh", "well"}
VOWEL = re.compile(r"[aeiouy]", re.I)


def looks_english(s: str) -> bool:
    if META.match(s) or len(s) < 6:
        return False
    words = [w.lower() for w in LATIN_WORD.findall(s)]
    if len(words) < 2:
        return False
    if not all(VOWEL.search(w) or w in FUNC for w in words):
        return False
    # 至少一个功能词或句末标点，排除 "uiview toggle" 这类资源名
    return any(w in FUNC for w in words) or s.rstrip()[-1] in ".!?…"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.path.join(ROOT, "GameLensData", "real_corpus.txt"))
    ap.add_argument("--limit", type=int, default=1200)
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--legacy", action="store_true",
                    help="退回改动前（旧词表 + 无缩写/主谓处理）的引擎，用于前后对比")
    ap.add_argument("--dump", action="store_true", help="把干净语料另存一份")
    a = ap.parse_args()

    raw = [l.strip() for l in open(a.corpus, encoding="utf-8").read().splitlines() if l.strip()]
    lines = [l for l in raw if looks_english(l)]
    print(f"原始 {len(raw)} 条 → 玩家可见文本 {len(lines)} 条")
    if a.dump:
        out = os.path.join(ROOT, "GameLensData", "real_corpus_clean.txt")
        open(out, "w", encoding="utf-8").write("\n".join(lines))
        print(f"已写出 {out}")
    lines = lines[: a.limit]

    eng = LiteMT()
    if a.legacy:
        from gamelens.translate import lite as L
        from gamelens.translate.lexicon import PHRASES
        from gamelens.translate.lexicon_extra import EXTRA_PHRASES
        for k in EXTRA_PHRASES:
            PHRASES.pop(k, None)
        L.PRON_RULE_RE = []
        L.expand_contractions = lambda s: s
        eng = LiteMT()

    full = part = none = 0
    residue: collections.Counter = collections.Counter()
    samples = []
    t0 = time.perf_counter()
    outs = eng.translate(lines)
    cost = (time.perf_counter() - t0) * 1000

    for src, dst in zip(lines, outs):
        dst = (dst or "").strip()
        cjk = len(CJK.findall(dst))
        dense = len(re.sub(r"\s", "", dst)) or 1
        ratio = cjk / dense
        left = [w for w in LATIN_WORD.findall(dst)]
        residue.update(w.lower() for w in left)
        if ratio >= 0.75 and not left:
            full += 1
        elif cjk:
            part += 1
            if len(samples) < 12:
                samples.append((src, dst))
        else:
            none += 1
            if len(samples) < 12:
                samples.append((src, dst))

    n = len(lines)
    print(f"语料 {n} 条，耗时 {cost:.0f} ms（{cost / max(1, n):.2f} ms/条）")
    print(f"完全汉化 {full:5d}  {full / n:6.1%}")
    print(f"部分汉化 {part:5d}  {part / n:6.1%}")
    print(f"未汉化   {none:5d}  {none / n:6.1%}")
    print("\n残留最多的英文词（前 40）：")
    print("  " + "  ".join(f"{w}×{c}" for w, c in residue.most_common(40)))
    print("\n抽样：")
    for src, dst in samples:
        print(f"  {src[:60]!r} → {dst[:60]!r}")

    data = {"tag": a.tag, "n": n, "full": full, "part": part, "none": none,
            "ms_per_line": round(cost / max(1, n), 3),
            "residue_top": residue.most_common(80)}
    old = {}
    if os.path.exists(REPORT):
        try:
            old = json.load(open(REPORT, encoding="utf-8"))
        except Exception:
            old = {}
    old[a.tag] = data
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(old, fh, ensure_ascii=False, indent=2)
    print(f"\n报告：{REPORT}（tag={a.tag}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
