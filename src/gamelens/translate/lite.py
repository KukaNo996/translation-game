"""LiteMT —— 内置离线轻量翻译引擎。

设计目标：零依赖、零网络、零显存，单条译文 < 1ms，专门对付游戏界面短句。
做法是"模板规则 + 最长短语匹配 + 形态还原 + 语序润色"的混合策略：

  1) 整句命中词表        → 直接返回（最常见，质量最好）
  2) 命中句型模板        → 递归翻译槽位后套模板（"Press X to Y" → "按 X 以 Y"）
  3) 最长短语切分        → 最多 5 词的前缀匹配，避免 sweep/start 之类被拆错
  4) 词形还原            → enemies→enemy, running→run, saved→save
  5) 中文粘连与标点转换  → 去掉多余空格，. ! ? , : → 。！？，：

未收录的词保留原文，所以输出会是"混合可读"而不是乱码——这也是离线兜底的
正确行为：能读懂的翻成中文，读不懂的至少还在原位。
"""
from __future__ import annotations

import re
from typing import List, Sequence

from .base import TranslateEngine
from .lexicon import (ADJECTIVES, CONTRACT_S, CONTRACTIONS, PHRASES,
                      PRONOUN_ZH, STOP_DROP, SUFFIX_RULES)

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’]*|\d+(?:[.,]\d+)*|\{\w*\}|\[\[\w+\]\]|%[sd]|\S")
PUNCT_MAP = {",": "，", ".": "。", "!": "！", "?": "？", ":": "：", ";": "；",
             "(": "（", ")": "）"}
PLACEHOLDER_RE = re.compile(r"^(?:\{\w*\}|\[\[\w+\]\]|%[sd]|[\d.,:/\-+%]+|x\d+)$")

ROMAN = {"i": "一", "ii": "二", "iii": "三", "iv": "四", "v": "五", "vi": "六",
         "vii": "七", "viii": "八", "ix": "九", "x": "十", "xi": "十一", "xii": "十二"}
CHAPTER_RE = re.compile(r"^chapter\s+([0-9]+|[ivxlcdm]+)\s*[：:·\-–—]?\s*(.*)$", re.I)

KEY_NAMES = {
    "tab": "Tab", "enter": "回车", "return": "回车", "esc": "Esc", "escape": "Esc",
    "shift": "Shift", "ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt",
    "space": "空格", "backspace": "退格", "delete": "Del", "del": "Del",
    "insert": "Ins", "home": "Home", "end": "End", "page up": "PageUp",
    "page down": "PageDown", "arrow keys": "方向键", "arrow key": "方向键",
    "up": "上", "down": "下", "left": "左", "right": "右",
    "lmb": "左键", "rmb": "右键", "mmb": "中键", "mouse wheel": "滚轮",
    "any key": "任意键", "any button": "任意按钮", "start button": "开始键",
}
for _i in range(1, 13):
    KEY_NAMES[f"f{_i}"] = f"F{_i}"

# (正则, 目标模板) —— 模板里 {1},{2} 表示对应捕获组的译文
TEMPLATES = [
    (r"^wave (\d+) of (\d+)$", "第 {1} / {2} 波"),
    (r"^are you sure (?:you want )?to (.+?)\??$", "你确定要{1}吗？"),
    (r"^(?:do|would|will) you (?:want|wish) to (.+?)\??$", "是否要{1}？"),
    (r"^would you like to (.+?)\??$", "要{1}吗？"),
    (r"^will you (.+?)\??$", "你愿意{1}吗？"),
    (r"^can you (.+?)\??$", "你能{1}吗？"),
    (r"^could you (.+?)\??$", "你能{1}吗？"),
    (r"^you (?:have|'ve) (.+)$", "你已{1}"),
    (r"^you must (.+)$", "你必须{1}"),
    (r"^you can (.+)$", "你可以{1}"),
    (r"^i (?:have|'ve) (.+)$", "我已经{1}"),
    (r"^press (.+?) to (.+?)$", "按{1}以{2}"),
    (r"^press (.+)$", "按下{1}"),
    (r"^(?:click|tap) (.+?) to (.+?)$", "点击{1}以{2}"),
    (r"^(?:click|tap) (.+)$", "点击{1}"),
    (r"^(.+?) (?:has|have) been (unlocked|unlock|opened|acquired|defeated|destroyed)$",
     "{1}已{2}"),
    (r"^(.+?) (?:is|are) (?:now )?(unlocked|available|ready|complete|completed)$",
     "{1}已{2}"),
    (r"^(.+?) (?:is|are) locked$", "{1}未解锁"),
    (r"^(.+?) (?:is|are) (full|empty|open|closed)$", "{1}已{2}"),
    (r"^(.+?) not enough$", "{1}不足"),
    (r"^not enough (.+)$", "{1}不足"),
    (r"^(.+?) (?:required|requires)$", "需要{1}"),
    (r"^(.+?) (?:acquired|obtained|received|collected)$", "获得{1}"),
    (r"^(\d+) ?(?:x|×) ?(.+)$", "{1} 个{2}"),
    (r"^(.+?) ?(?:x|×) ?(\d+)$", "{1} ×{2}"),
    (r"^level ?(\d+)$", "等级 {1}"),
    (r"^(.+?) level (\d+)$", "{1} 等级 {2}"),
    (r"^(\d+) ?(s|sec|secs|seconds?)$", "{1} 秒"),
    (r"^(\d+) ?(m|min|mins|minutes?)$", "{1} 分钟"),
    (r"^(\d+) ?(h|hr|hrs|hours?)$", "{1} 小时"),
    (r"^(\d+) ?(d|days?)$", "{1} 天"),
    (r"^(\d+) ?/\s?(\d+)$", "{1} / {2}"),
    (r"^(.+?) ?\((\d+)\)$", "{1}（{2}）"),
    (r"^(.+?) of (.+)$", "{2}之{1}"),
    (r"^(.+?) (?:damage|dmg)$", "{1}伤害"),
    (r"^(?:attack|attacks) (.+)$", "攻击{1}"),
    (r"^(.+?) (?:attacks|is attacking)!?$", "{1}发起攻击！"),
    (r"^(.+?) (?:defeated|killed|slain)$", "{1}被击败"),
    (r"^you (?:got|obtained|received|found) (.+)$", "你获得了{1}"),
    (r"^you (?:need|require) (\d+)$", "你需要 {1}"),
    (r"^open (.+)$", "打开{1}"),
    (r"^talk to (.+)$", "与{1}交谈"),
    (r"^speak to (.+)$", "与{1}说话"),
    (r"^use (.+)$", "使用{1}"),
    (r"^equip (.+)$", "装备{1}"),
    (r"^buy (.+)$", "购买{1}"),
    (r"^sell (.+)$", "出售{1}"),
    (r"^(.+?) (?:unlocked|unlock)$", "解锁{1}"),
    (r"^(.+?) (?:saved|save)$", "保存{1}"),
    (r"^(.+?) (?:failed|fail)$", "{1}失败"),
    (r"^(.+?) (?:success|succeeded|successful)$", "{1}成功"),
]

TEMPLATE_RE = [(re.compile(p, re.I), t) for p, t in TEMPLATES]

# ---------------------------------------------------------------- 缩写展开
# 游戏对话里 "don't / it's / I'm" 占极大比例，逐词查表必然崩（"don't" 查不到）。
# 先把缩写展开成完整形式，再交给上面的句型模板与下面的主谓模板处理。
_CONTRACT_RE = re.compile(r"\b([A-Za-z]+)(n't|'re|'ll|'ve|'m|'d|'s)\b", re.I)
_SIMPLE = {"'re": "are", "'ll": "will", "'ve": "have", "'m": "am", "'d": "would"}


def expand_contractions(text: str) -> str:
    if "'" not in text and "’" not in text:
        return text
    t = text.replace("’", "'")

    def _repl(m: re.Match) -> str:
        head, suf = m.group(1), m.group(2)
        whole = (head + suf).lower()
        if whole in CONTRACT_S:          # it's → it is（其余 's 当所有格，不动）
            return CONTRACT_S[whole]
        if whole in CONTRACTIONS:        # can't → can not
            return CONTRACTIONS[whole]
        if suf.lower() == "n't":
            return head + " not"
        base = _SIMPLE.get(suf.lower())
        return head + " " + base if base else m.group(0)

    return _CONTRACT_RE.sub(_repl, t)


# ---------------------------------------------------------------- 主谓句型
_PRON = r"(i|you|he|she|it|we|they)"
PRON_RULES = [
    (re.compile(rf"^{_PRON} (?:am|is|are|was|were) not (.+)$", re.I), "neg_be"),
    (re.compile(rf"^{_PRON} (?:am|is|are|was|were) (.+)$", re.I), "be"),
    (re.compile(rf"^{_PRON} (?:(?:do|does|did) not|never) (.+)$", re.I), "neg_do"),
    (re.compile(rf"^{_PRON} (?:can|could) not (.+)$", re.I), "neg_can"),
    (re.compile(rf"^{_PRON} (?:will|would) not (.+)$", re.I), "neg_will"),
    (re.compile(rf"^{_PRON} (?:must|have to|has to|had to|need to|needs to) (.+)$", re.I), "must"),
    (re.compile(rf"^{_PRON} (?:should|ought to) (.+)$", re.I), "should"),
    (re.compile(rf"^{_PRON} (?:will|would|shall|am going to|is going to|are going to) (.+)$",
                re.I), "will"),
    (re.compile(rf"^{_PRON} (?:can|could) (.+)$", re.I), "can"),
    (re.compile(rf"^{_PRON} (?:want|wants|wanted|need|needs|needed|like|likes|liked|love|loves)"
                rf" (?:to )?(.+)$", re.I), "want"),
    (re.compile(rf"^{_PRON} (?:have|has|had) (.+)$", re.I), "have"),
    (re.compile(rf"^(?:do|does|did) {_PRON} (.+)$", re.I), "q_do"),
    (re.compile(rf"^(?:is|are|was|were) {_PRON} (.+)$", re.I), "q_be"),
    (re.compile(r"^(?:this|that) (?:is|was) (.+)$", re.I), "this_is"),
    (re.compile(r"^there (?:is|are|was|were) (.+)$", re.I), "there"),
    (re.compile(r"^let us (.+)$", re.I), "let_us"),
    (re.compile(rf"^{_PRON} (.+)$", re.I), "plain"),
]
PRON_RULE_RE = [(rx, kind) for rx, kind in PRON_RULES]


def _is_adj_first(text: str) -> bool:
    words = [w.lower().strip(".,!?") for w in text.split() if w.strip()]
    return bool(words) and words[0] in ADJECTIVES

VERB_PAST = {
    "taken": "拿走", "gone": "去", "seen": "看到", "done": "做", "made": "制造",
    "found": "找到", "given": "给予", "become": "成为", "come": "来",
    "broken": "破坏", "opened": "打开", "destroyed": "摧毁", "defeated": "击败",
    "unlocked": "解锁", "left": "离开", "died": "死亡", "won": "赢得",
    "lost": "失去", "finished": "完成", "started": "开始", "killed": "杀死",
    "eaten": "吃掉", "hidden": "藏起", "stolen": "偷走", "cursed": "诅咒",
    "repaired": "修理", "reached": "到达", "cleared": "清理", "sold": "出售",
    "bought": "购买", "chosen": "选择", "built": "建造", "sent": "派遣",
    "written": "书写", "spoken": "说", "heard": "听到", "kept": "保留",
    "met": "遇见", "paid": "支付", "told": "告诉", "thought": "认为",
    "understood": "理解", "woken": "醒来", "fallen": "倒下", "fled": "逃离",
    "grown": "成长", "shown": "展示", "thrown": "投掷", "worn": "穿戴",
    "released": "释放", "unleashed": "释放", "captured": "捕获",
    "saved": "救下", "escaped": "逃脱", "arrived": "抵达", "awakened": "苏醒",
    "returned": "归来", "answered": "回答", "completed": "完成",
}

SLOT_RE = re.compile(r"\{(\d)\}")


def _fill(tpl: str, slots: dict) -> str:
    out = SLOT_RE.sub(lambda m: slots.get(m.group(1), ""), tpl)
    # 中英混排空格：按 Tab 打开地图 / 等级 12
    out = re.sub(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", out)
    out = re.sub(r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out


def _norm(text: str) -> str:
    t = text.strip()
    t = t.replace("’", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"\s+", " ", t)
    return t


def _lemma(word: str) -> str:
    w = word.lower()
    if w in PHRASES:
        return w
    for suf, rep in SUFFIX_RULES:
        if w.endswith(suf) and len(w) > len(suf) + 2:
            cand = w[: -len(suf)] + rep
            if cand in PHRASES:
                return cand
            if suf == "s" and not w.endswith("ss") and w[:-1] in PHRASES:
                return w[:-1]
    return w


def _tokenize(text: str) -> List[str]:
    return TOKEN_RE.findall(text)


def _is_latin(tok: str) -> bool:
    return bool(tok) and (tok[0].isascii() and tok[0].isalnum())


def _split_merged(word: str):
    """把 OCR 粘在一起的英文词按词表贪心切开，切不开就返回空。"""
    w = word.lower()
    if len(w) < 6 or not w.isalpha():
        return []
    out, i = [], 0
    while i < len(w):
        for n in range(min(12, len(w) - i), 2, -1):
            piece = w[i:i + n]
            if piece in PHRASES:
                out.append(piece)
                i += n
                break
        else:
            return []
    return out if len(out) >= 2 else []


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f"


class LiteMT(TranslateEngine):
    name = "litemt"
    label = "LiteMT 内置离线引擎"
    offline = True
    supports_batch = True
    max_batch = 128

    def translate(self, texts: Sequence[str], src: str = "en", dst: str = "zh") -> List[str]:
        return [self.one(t) for t in texts]

    # ------------------------------------------------------------------
    def _clause(self, text: str, depth: int) -> str:
        """主谓句型：I don't know → 我不知道；you're tired → 你累了。"""
        for rx, kind in PRON_RULE_RE:
            m = rx.match(text)
            if not m:
                continue
            if kind in ("q_do", "q_be"):
                pron, comp = m.group(1), m.group(2)
            elif kind in ("this_is", "there", "let_us"):
                pron, comp = "", m.group(1)
            else:
                pron, comp = m.group(1), m.group(2)
            comp = comp.strip()
            if not comp:
                continue
            sub = self.one(comp, depth + 1) if comp else ""
            # 没翻出中文就放弃，免得产出 "你know" 这种半英半中
            if not sub or not any("\u4e00" <= ch <= "\u9fff" for ch in sub):
                continue
            s = PRONOUN_ZH.get(pron.lower(), pron)
            adj = _is_adj_first(comp)
            if kind == "plain" and depth > 1:
                continue          # 递归里只用精确的模板，避免过度套用
            head = {
                "neg_be": f"{s}不", "be": f"{s}很" if adj else f"{s}是",
                "neg_do": f"{s}不", "neg_can": f"{s}不能",
                "neg_will": f"{s}不会", "must": f"{s}必须",
                "should": f"{s}应该", "will": f"{s}会", "can": f"{s}能",
                "want": f"{s}想", "have": f"{s}有", "plain": s,
            }.get(kind)
            if kind == "this_is":
                out = ("这是" if m.group(0).lower().startswith("this") else "那是") + sub
            elif kind == "there":
                out = "有" + sub
            elif kind == "let_us":
                out = "让我们" + sub
            elif kind == "q_do":
                out = f"{s}{sub}吗"
            elif kind == "q_be":
                out = (f"{s}{sub}吗" if adj else f"{s}是{sub}吗")
            elif head is not None:
                out = head + sub
            else:
                continue
            return out
        return ""

    def one(self, text: str, depth: int = 0) -> str:
        raw = _norm(text)
        if not raw:
            return ""
        raw = expand_contractions(raw)
        low = raw.lower().strip(" .!。！")

        # 1) 整句命中
        if low in PHRASES:
            return self._punct(raw, PHRASES[low])

        # 2) 键位名 / 纯占位符 / 纯数字
        if low in KEY_NAMES:
            return KEY_NAMES[low]
        if PLACEHOLDER_RE.match(raw):
            return raw

        # 3) 章节标题：Chapter I : The Broken Seal → 第一章：破碎的封印
        m = CHAPTER_RE.match(low)
        if m:
            num = m.group(1)
            num = ROMAN.get(num, num)
            rest = self.one(m.group(2), depth + 1) if m.group(2).strip() else ""
            head = f"第{num}章"
            return self._punct(raw, head + (f"：{rest}" if rest else ""))

        # 4) 句型模板
        if depth < 3:
            # 用保留大小写的原文去匹配，这样像 "Press START to ..." 里的键位能原样保留
            match_text = raw.strip(" .!。！？?")
            for rx, tpl in TEMPLATE_RE:
                m = rx.match(match_text) or rx.match(low)
                if m is None:
                    continue
                if not m:
                    continue
                slots = {}
                ok = True
                for idx, g in enumerate(m.groups(), start=1):
                    if g is None:
                        slots[str(idx)] = ""
                        continue
                    trimmed = g.strip(" .,!?")
                    if ("以" in tpl and trimmed.isupper()
                            and 0 < len(trimmed) <= 8 and " " not in trimmed):
                        slots[str(idx)] = trimmed      # 全大写短词 = 键位/代号，保留原文
                        continue
                    if trimmed in ("not enough",):
                        ok = False
                        break
                    sub = self.one(trimmed, depth + 1) if trimmed else ""
                    if not sub:
                        ok = False
                        break
                    if "已" in tpl and sub.startswith("已"):
                        sub = sub[1:]        # 避免 "已已解锁"
                    slots[str(idx)] = sub
                if not ok:
                    continue
                out = _fill(tpl, slots)
                if not out.strip():
                    continue
                return self._punct(raw, out)

        # 5) 主谓句型（缩写展开之后才吃得下 don't / I'm 这类）
        if depth < 3:
            got = self._clause(raw.strip(" .!。！？?"), depth)
            if got:
                return self._punct(raw, got)

        # 5) 短语优先的逐词翻译
        tokens = _tokenize(raw)
        parts: List[tuple] = []      # (译文片段, 是否为西文)
        i = 0
        unknown = 0
        total = 0
        while i < len(tokens):
            nxt = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
            cur = tokens[i].lower()
            if cur in ("has", "have", "had") and nxt in VERB_PAST:
                parts.append(("已经" + VERB_PAST[nxt] + "了", False))
                total += 2
                i += 2
                continue
            if cur in ("will", "can", "could", "would", "do", "did") and nxt == "you":
                parts.append(({"will": "你愿意", "can": "你能", "could": "你能",
                               "would": "你会", "do": "你", "did": "你"}[cur], False))
                total += 2
                i += 2
                continue
            if cur == "will" and nxt in VERB_PAST:
                parts.append(("将要" + VERB_PAST[nxt], False))
                total += 2
                i += 2
                continue
            matched = False
            for n in range(min(5, len(tokens) - i), 0, -1):
                gram_tokens = tokens[i:i + n]
                gram = " ".join(gram_tokens).lower()
                if gram in PHRASES and not (n == 1 and gram in STOP_DROP):
                    if n == 1 and not gram_tokens[0][0].isalpha():
                        continue
                    parts.append((PHRASES[gram], False))
                    total += n
                    i += n
                    matched = True
                    break
            if matched:
                continue
            tok = tokens[i]
            i += 1
            total += 1
            # 所有格：Aino's room → Aino的房间（人名保留，关系词补上）
            if tok.lower().endswith("'s") and len(tok) > 2 and tok[:-2].isalpha():
                base = self.one(tok[:-2], depth + 1)
                if base and any("\u4e00" <= ch <= "\u9fff" for ch in base):
                    parts.append((base + "的", False))
                    continue
            # OCR 常在装饰图案/描边上切出孤立单字母（e / y / p …），直接丢掉
            if len(tok) == 1 and tok.isalpha() and tok.lower() not in ("a", "i") \
                    and tok.lower() not in KEY_NAMES:
                unknown += 1
                continue
            if re.fullmatch(r"[xX]\d+", tok):
                parts.append(("×" + tok[1:], True))
                continue
            if not tok[0].isalpha():
                if tok in PUNCT_MAP:
                    parts.append((PUNCT_MAP[tok], False))
                else:
                    parts.append((tok, True))
                continue
            if PLACEHOLDER_RE.match(tok):
                parts.append((tok, True))
                continue
            lem = _lemma(tok.strip("'"))
            if lem in PHRASES:
                parts.append((PHRASES[lem], False))
            elif tok.lower() in KEY_NAMES:
                parts.append((KEY_NAMES[tok.lower()], True))
            elif tok.lower() in STOP_DROP:
                continue
            elif lem not in PHRASES:
                pieces = _split_merged(tok)
                if pieces:
                    for piece in pieces:
                        parts.append((PHRASES[piece], False))
                        total += 1
                else:
                    parts.append((tok, True))   # 未收录，保留原文
                    unknown += 1
            else:
                parts.append((PHRASES[lem], False))

        out = self._join(parts)
        out = self._punct(raw, out)
        if unknown and unknown >= max(2, total // 2) and not any(
                "\u4e00" <= ch <= "\u9fff" for ch in out):
            # 基本没翻出来：交给上层决定是否走网络引擎
            return ""
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _join(parts: List[tuple]) -> str:
        tight = set("，。！？：；）%、…×〉》·/")
        openers = set("（〈《")
        buf = ""
        prev_latin = False
        for text, latin in parts:
            if not text:
                continue
            if buf:
                prev_ch, cur_ch = buf[-1], text[0]
                space = False
                if cur_ch in tight or prev_ch in openers:
                    space = False
                elif cur_ch in ("+", "-"):
                    space = True
                elif prev_ch in ("+", "-", "/"):
                    space = False
                elif latin and prev_latin and prev_ch.isascii() and prev_ch.isalnum():
                    space = True
                elif latin and not cur_ch.isdigit() and _is_cjk(prev_ch) and cur_ch.isalpha():
                    space = True
                elif latin and cur_ch.isdigit() and _is_cjk(prev_ch):
                    space = True
                elif _is_cjk(cur_ch) and prev_ch.isdigit():
                    space = True
                if space:
                    buf += " "
            buf += text
            prev_latin = latin
        return buf

    @staticmethod
    def _punct(raw: str, out: str) -> str:
        out = out.strip()
        out = re.sub(r"\s+([，。！？：；）])", r"\1", out)
        out = re.sub(r"([（])\s+", r"\1", out)
        out = re.sub(r"(的){2,}", "的", out)
        out = re.sub(r"\s{2,}", " ", out)
        tail = raw.rstrip()
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in out)
        if tail.endswith(("...", "…", "..")):
            if out and not out.endswith(("…", "。")):
                out = out.rstrip("。") + "…"
            return out
        if tail.endswith(("?", "？")):
            if not out.endswith(("？", "?")):
                out += "？"
            if out.endswith("？") and "你愿意" in out and "吗" not in out:
                out = out[:-1] + "吗？"
        elif tail.endswith(("!", "！")):
            if not out.endswith(("！", "!")):
                out += "！"
        elif tail.endswith((".", "。")) and has_cjk and len(out) > 3:
            if not out.endswith(("。", "！", "？", "…", "!", "?", ".")):
                out += "。"
        return out
