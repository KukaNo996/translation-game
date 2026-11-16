"""从真实游戏资源里抽取文本语料，作为"汉化覆盖率"的客观基准。

用法：
    python tools/extract_corpus.py --data "E:/Game/.../Game_Data" --out GameLensData/real_corpus.txt
"""
from __future__ import annotations

import argparse
import collections
import os
import re

RUN = re.compile(rb"[ -~]{4,160}")
WORD = re.compile(r"[A-Za-z][A-Za-z']*")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="游戏的 *_Data 目录")
    ap.add_argument("--out", default="GameLensData/real_corpus.txt")
    ap.add_argument("--max", type=int, default=4000)
    a = ap.parse_args()

    files = []
    for root, _dirs, names in os.walk(a.data):
        for n in names:
            if n.endswith((".assets", ".resource", ".bytes", ".json", ".txt")):
                p = os.path.join(root, n)
                try:
                    if os.path.getsize(p) < 400 * 1024 * 1024:
                        files.append(p)
                except OSError:
                    pass

    cand: collections.Counter = collections.Counter()
    for f in files:
        try:
            data = open(f, "rb").read()
        except OSError:
            continue
        for m in RUN.finditer(data):
            s = m.group().decode("ascii", "ignore").strip()
            if len(s) < 4 or len(s) > 60:
                continue
            words = WORD.findall(s)
            if not (1 <= len(words) <= 9):
                continue
            ratio = sum(c.isalpha() or c.isspace() for c in s) / len(s)
            if ratio < 0.78:
                continue
            low = s.lower()
            if low.startswith(("http", "www", "unity", "shader", "assets/", "guid")):
                continue
            if any(ch in s for ch in "{}<>\\|_=/"):
                continue
            # 只要"像人话"的串：至少两个词、含小写实词、不是 CamelCase 字段名
            if " " not in s:
                continue
            if not any(w.islower() and len(w) >= 2 for w in words):
                continue
            cand[s] += 1

    top = cand.most_common(a.max)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(s for s, _ in top))
    print(f"扫描文件 {len(files)} 个，唯一字符串 {len(cand)}，写入 {len(top)} 条 → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
