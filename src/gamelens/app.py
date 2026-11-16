"""GameLens 主界面。"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Dict, List, Optional

from . import __version__, winapi
from .config import CONFIG_PATH, Config, app_dir
from .hotkeys import HotkeyManager
from .overlay import OverlayWindow, Sprite
from .pipeline import Pipeline, Status
from .selector import RegionSelector
from .theme import (ACCENT, BG, BORDER, Card, FG, FONT_MONO, FONT_UI, MUTED, OK,
                    PANEL, PANEL2, WARN, apply_theme)
from .translate import PROVIDER_FIELDS, PROVIDER_LABELS, TranslationService

MODE_META = {
    "region": ("屏幕选区汉化", "只汉化你框选的那块区域（对话框、任务栏、字幕条）。\n"
                              "鼠标键盘事件全部穿透，点原按钮位置照样生效。"),
    "inject": ("内部注入式汉化", "接管游戏进程内存里的文本缓冲区，直接写入译文。\n"
                                "游戏自己渲染中文，效果最原生，但需要管理员权限。"),
    "fullscreen": ("全屏汉化", "整屏识别与翻译，适合全屏文字多的游戏。\n"
                              "同样是完全穿透，不影响任何操作。"),
}

FIELD_LABELS = {
    "base_url": "接口地址",
    "api_key": "API Key / 令牌",
    "model": "模型名称",
    "temperature": "温度",
    "timeout": "超时（秒）",
    "secret_key": "密钥 Secret",
}

PROVIDER_HINTS = {
    "litemt": "内置离线引擎：词表 + 句型模板 + 形态还原，零网络零显存，单条 <1ms。\n"
              "适合菜单、按钮、短提示，是最稳的兜底方案。",
    "ollama": "本机跑大模型：先 `ollama serve`，再 `ollama pull qwen2.5:7b-instruct`。\n"
              "地址填 http://127.0.0.1:11434/v1 即可，无需联网。",
    "deepseek": "https://api.deepseek.com/v1 ，模型 deepseek-chat，OpenAI 兼容协议。",
    "openai": "官方或任意 OpenAI 兼容网关，模型如 gpt-4o-mini。",
    "siliconflow": "硅基流动，国内直连，如 Qwen/Qwen2.5-7B-Instruct。",
    "custom": "自建 vLLM / One-API / LM Studio / 中转站，只要兼容 /chat/completions。",
    "google": "免密钥，直接可用；高频请求会被限流，适合快速试效果。",
    "deepl": "免费版地址 https://api-free.deepl.com/v2 ，专业版去掉 -free。",
    "baidu": "百度翻译开放平台，需要 appid 与密钥，标准版有免费额度。",
}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = Config()
        self.title(f"GameLens 游戏实时汉化  v{__version__}")
        self.configure(bg=BG)
        self.minsize(1040, 720)
        try:
            self.iconbitmap(default="")
        except Exception:
            pass
        apply_theme(self)
        self._dpi = winapi.system_dpi()
        try:
            self.tk.call("tk", "scaling", self._dpi / 72.0)
        except Exception:
            pass

        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.service = TranslationService(self.cfg, self.log)
        self.overlay = OverlayWindow(
            self,
            font_family=self.cfg.get("style", {}).get("font_family", "Microsoft YaHei UI"),
            font_scale=float(self.cfg.get("style", {}).get("font_scale", 0.62)),
            mask=bool(self.cfg.get("style", {}).get("mask", True)),
            mask_pad=int(self.cfg.get("style", {}).get("mask_pad", 2)),
            stroke=bool(self.cfg.get("style", {}).get("stroke", False)),
            stipple=str(self.cfg.get("style", {}).get("stipple", "")),
        )
        self.pipeline = Pipeline(self.cfg, self.service, self._on_frame,
                                 self._on_status, self.log)
        self.pipeline.hide_overlay = self._grab_without_overlay
        self.selector: Optional[RegionSelector] = None
        self._windows: List[winapi.WindowInfo] = []
        self._pw_vars: Dict[str, tk.StringVar] = {}
        self._pages: Dict[str, tk.Frame] = {}
        self._nav_buttons: Dict[str, tk.Label] = {}
        self._active_page = "console"
        self._status_note = tk.StringVar(value="就绪")
        self._mode_var = tk.StringVar(value=self.cfg.get("mode", "region"))
        self._probe_busy = False
        self._probe_started_here = True
        self._probe_deadline = 0.0
        self._stopping = False

        self._build_ui()
        self._fit_window()
        self._register_hotkeys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._drain_queue)
        self.after(400, self._tick_clock)
        self.log(f"GameLens v{__version__} 启动；配置目录 {app_dir()}")
        if not winapi.is_admin():
            self.log("提示：未以管理员身份运行，注入模式可能无法打开部分游戏进程。")
        self._refresh_windows()
        if (self.cfg.get("region") or [0, 0, 0, 0])[2] < 16:
            self._status_note.set("第一步：选好窗口后点「锁定游戏窗口」")
            self.log("还没有汉化区域：在下拉框选中游戏窗口，点「锁定游戏窗口」即可自动取景。")

    def _grab_without_overlay(self, grab):
        """抓屏前把叠加层藏一下：否则 OCR 会把自己画的中文又读进去。"""
        hwnd = winapi.hwnd_of(self.overlay.win) if self.overlay.visible else 0
        if not hwnd:
            return grab()
        winapi.set_visible(hwnd, False)
        try:
            time.sleep(0.012)
            return grab()
        finally:
            winapi.set_visible(hwnd, True)
            winapi.keep_topmost(hwnd, True)

    # ================================================================== 通用
    def log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.ui_queue.put(("log", f"[{stamp}] {msg}"))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "frame":
                    self._apply_frame(*payload)
                elif kind == "status":
                    self._apply_status(payload)
                elif kind == "call":
                    payload()
                elif kind == "toast":
                    self._status_note.set(payload)
        except queue.Empty:
            pass
        self.after(50, self._drain_queue)

    def _tick_clock(self) -> None:
        running = self.pipeline.running
        self._pill.configure(text="● 运行中" if running else "● 空闲",
                             fg=OK if running else MUTED)
        self.after(500, self._tick_clock)

    # ================================================================== 布局
    def _build_ui(self) -> None:
        header = tk.Frame(self, bg=BG, height=64)
        header.pack(side="top", fill="x", padx=0, pady=0)
        header.pack_propagate(False)
        tk.Label(header, text="GameLens", bg=BG, fg=FG,
                 font=(FONT_UI, 19, "bold")).pack(side="left", padx=(22, 8), pady=14)
        tk.Label(header, text="游戏界面实时汉化 · 选区 / 注入 / 全屏", bg=BG, fg=MUTED,
                 font=(FONT_UI, 10)).pack(side="left", pady=(20, 0))
        self._pill = tk.Label(header, text="● 空闲", bg=BG, fg=MUTED,
                              font=(FONT_UI, 10, "bold"))
        self._pill.pack(side="right", padx=(0, 24))
        tk.Label(header, textvariable=self._status_note, bg=BG, fg=MUTED,
                 font=(FONT_UI, 9)).pack(side="right", padx=8)

        tk.Frame(self, bg=BORDER, height=1).pack(side="top", fill="x")

        body = tk.Frame(self, bg=BG)
        body.pack(side="top", fill="both", expand=True)

        side = tk.Frame(body, bg=PANEL, width=186)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        for key, label in (("console", "控制台"), ("engine", "翻译引擎"),
                           ("glossary", "术语表"), ("log", "运行日志"),
                           ("help", "使用说明")):
            item = tk.Label(side, text=f"   {label}", bg=PANEL, fg=MUTED,
                            font=(FONT_UI, 11), anchor="w", padx=12, pady=11)
            item.pack(fill="x", padx=10, pady=(6, 0))
            item.bind("<Button-1>", lambda e, k=key: self._show_page(k))
            item.bind("<Enter>", lambda e, w=item, k=key: self._nav_hover(w, k, True))
            item.bind("<Leave>", lambda e, w=item, k=key: self._nav_hover(w, k, False))
            self._nav_buttons[key] = item

        tk.Label(side, text="v" + __version__, bg=PANEL, fg="#4b5361",
                 font=(FONT_UI, 9)).pack(side="bottom", pady=12)

        self._content = tk.Frame(body, bg=BG)
        self._content.pack(side="left", fill="both", expand=True, padx=18, pady=14)

        self._pages["console"] = self._build_console(self._content)
        self._pages["engine"] = self._build_engine(self._content)
        self._pages["glossary"] = self._build_glossary(self._content)
        self._pages["log"] = self._build_log(self._content)
        self._pages["help"] = self._build_help(self._content)
        self._show_page("console")
        self._on_mode_change()

    def _fit_window(self) -> None:
        """按内容实际需要定窗口大小，适配各种缩放比例，避免卡片被截断。"""
        self.update_idletasks()
        w = self.winfo_reqwidth() + 10
        h = self.winfo_reqheight() + 10
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(w, sw - 40), min(h, sh - 80)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.minsize(min(1040, w), min(720, h))

    def _nav_hover(self, widget: tk.Label, key: str, on: bool) -> None:
        if key == self._active_page:
            return
        widget.configure(bg=PANEL2 if on else PANEL)

    def _show_page(self, key: str) -> None:
        self._active_page = key
        for k, w in self._nav_buttons.items():
            if k == key:
                w.configure(bg=PANEL2, fg=FG, font=(FONT_UI, 11, "bold"))
            else:
                w.configure(bg=PANEL, fg=MUTED, font=(FONT_UI, 11))
        for k, page in self._pages.items():
            if k == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()

    def _card(self, parent, title: str, sub: str = "") -> tk.Frame:
        card = Card(parent)
        card.pack(fill="x", pady=(0, 12))
        head = tk.Frame(card, bg=PANEL)
        head.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(head, text=title, bg=PANEL, fg=FG,
                 font=(FONT_UI, 11, "bold")).pack(side="left")
        if sub:
            tk.Label(head, text=sub, bg=PANEL, fg=MUTED,
                     font=(FONT_UI, 9)).pack(side="left", padx=8)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        return inner

    # ================================================================== 控制台
    def _build_console(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=BG)

        modes = tk.Frame(page, bg=BG)
        modes.pack(fill="x", pady=(0, 12))
        self._mode_cards: Dict[str, tk.Frame] = {}
        for idx, (key, (title, desc)) in enumerate(MODE_META.items()):
            card = Card(modes, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
            card.pack(side="left", fill="both", expand=True,
                      padx=(0 if idx == 0 else 10, 0))
            inner = tk.Frame(card, bg=PANEL)
            inner.pack(fill="both", expand=True, padx=14, pady=12)
            rb = tk.Radiobutton(inner, text=title, value=key, variable=self._mode_var,
                                command=self._on_mode_change, bg=PANEL, fg=FG,
                                activebackground=PANEL, activeforeground=FG,
                                selectcolor=PANEL2, font=(FONT_UI, 11, "bold"),
                                anchor="w", bd=0, highlightthickness=0, cursor="hand2")
            rb.pack(fill="x")
            tk.Label(inner, text=desc, bg=PANEL, fg=MUTED, font=(FONT_UI, 9),
                     justify="left", anchor="w", wraplength=300).pack(fill="x", pady=(6, 0))
            for w in (inner, card):
                w.bind("<Button-1>", lambda e, k=key: self._select_mode(k))
            self._mode_cards[key] = card

        cols = tk.Frame(page, bg=BG)
        cols.pack(fill="both", expand=True)
        left = tk.Frame(cols, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right = tk.Frame(cols, bg=BG, width=300)
        right.pack(side="left", fill="y", padx=(6, 0))
        right.pack_propagate(False)

        # --- 运行控制
        ctl = self._card(left, "运行控制", "① 锁定窗口　② 试试看　③ 开始汉化")
        btns = tk.Frame(ctl, bg=PANEL)
        btns.pack(fill="x")
        self._btn_start = ttk.Button(btns, text="开始汉化", style="Accent.TButton",
                                     command=self._start)
        self._btn_start.pack(side="left")
        self._btn_stop = ttk.Button(btns, text="停止并还原", command=self._stop)
        self._btn_stop.pack(side="left", padx=8)
        self._btn_probe = ttk.Button(btns, text="试试看", command=self._probe_region)
        self._btn_probe.pack(side="left")
        ttk.Button(btns, text="清空译文记忆", command=self._reset_memory).pack(side="left", padx=8)
        self._run_hint = tk.Label(ctl, text="", bg=PANEL, fg=MUTED, font=(FONT_UI, 9),
                                  justify="left", anchor="w", wraplength=580)
        self._run_hint.pack(fill="x", pady=(10, 0))

        # --- 区域
        reg = self._card(left, "汉化区域", "选区/全屏模式使用")
        row = tk.Frame(reg, bg=PANEL)
        row.pack(fill="x")
        self._region_label = tk.Label(row, text=self._region_text(), bg=PANEL, fg=FG,
                                      font=(FONT_MONO, 10), anchor="w")
        self._region_label.pack(side="left")
        ttk.Button(row, text="锁定游戏窗口", command=self._lock_window).pack(side="right")
        ttk.Button(row, text="框选区域", command=self._pick_region).pack(side="right", padx=6)
        ttk.Button(row, text="整屏", command=self._pick_fullscreen).pack(side="right")
        tk.Label(reg, text="「锁定游戏窗口」会直接取下拉框里那个窗口的画面范围，最省事；"
                           "框选时游戏窗口请保持窗口化/无边框全屏，独占全屏下叠加层不会显示。",
                 bg=PANEL, fg=MUTED, font=(FONT_UI, 9), anchor="w",
                 justify="left", wraplength=560).pack(fill="x", pady=(8, 0))

        # --- 注入目标
        inj = self._card(left, "注入目标进程", "仅注入模式使用")
        row2 = tk.Frame(inj, bg=PANEL)
        row2.pack(fill="x")
        self._proc_var = tk.StringVar(value="（未选择）")
        self._proc_combo = ttk.Combobox(row2, textvariable=self._proc_var, state="readonly",
                                        width=50)
        self._proc_combo.pack(side="left", fill="x", expand=True)
        self._proc_combo.bind("<<ComboboxSelected>>", self._on_proc_pick)
        ttk.Button(row2, text="刷新", command=self._refresh_windows).pack(side="left", padx=6)
        self._dry_var = tk.BooleanVar(value=bool(self.cfg.get("inject", {}).get("dry_run", True)))
        tk.Checkbutton(inj, text="演练模式（只定位、不写入内存，安全）",
                       variable=self._dry_var, bg=PANEL, fg=FG, selectcolor=PANEL2,
                       activebackground=PANEL, activeforeground=FG, bd=0,
                       highlightthickness=0,
                       command=self._on_dry_toggle).pack(anchor="w", pady=(8, 0))
        tk.Label(inj, text="注入会改写游戏内存，存在崩溃风险；停止时会自动还原。"
                           "找得到文本才有效，GPU 纹理文字需靠叠加层兜底。",
                 bg=PANEL, fg=WARN, font=(FONT_UI, 9), anchor="w",
                 justify="left", wraplength=560).pack(fill="x", pady=(4, 0))

        # --- 识别参数
        rec = self._card(left, "识别与刷新", "")
        grid = tk.Frame(rec, bg=PANEL)
        grid.pack(fill="x")
        tk.Label(grid, text="刷新间隔", bg=PANEL, fg=MUTED, font=(FONT_UI, 9)).grid(
            row=0, column=0, sticky="w")
        self._interval_var = tk.IntVar(value=int(self.cfg.get("interval_ms", 450)))
        scale = ttk.Scale(grid, from_=150, to=2000, orient="horizontal",
                          variable=self._interval_var,
                          command=lambda *_: self._on_interval())
        scale.grid(row=0, column=1, sticky="ew", padx=10)
        self._interval_label = tk.Label(grid, text=f"{self._interval_var.get()} ms",
                                        bg=PANEL, fg=FG, font=(FONT_MONO, 9), width=8)
        self._interval_label.grid(row=0, column=2, sticky="e")
        grid.columnconfigure(1, weight=1)

        tk.Label(grid, text="OCR 引擎", bg=PANEL, fg=MUTED, font=(FONT_UI, 9)).grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        self._ocr_var = tk.StringVar()
        ocr_labels = {"auto": "自动（推荐）", "winrt": "Windows 系统 OCR",
                      "rapid": "RapidOCR 离线模型"}
        self._ocr_combo = ttk.Combobox(
            grid, state="readonly", width=22,
            values=list(ocr_labels.values()))
        self._ocr_combo.set(ocr_labels.get(self.cfg.get("ocr_engine", "auto"), "自动（推荐）"))
        self._ocr_combo.grid(row=1, column=1, sticky="w", padx=10, pady=(8, 0))
        self._ocr_combo.bind("<<ComboboxSelected>>", lambda e: self._on_ocr_change(ocr_labels))

        tk.Label(grid, text="游戏语言", bg=PANEL, fg=MUTED, font=(FONT_UI, 9)).grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        lang_labels = {"en": "英文（识别率最高）", "ja": "日文",
                       "auto": "自动（中英混排）"}
        self._lang_combo = ttk.Combobox(grid, state="readonly", width=22,
                                        values=list(lang_labels.values()))
        self._lang_combo.set(lang_labels.get(self.cfg.get("source_lang", "en"),
                                             "英文（识别率最高）"))
        self._lang_combo.grid(row=2, column=1, sticky="w", padx=10, pady=(8, 0))
        self._lang_combo.bind("<<ComboboxSelected>>",
                              lambda e: self._on_lang_change(lang_labels))

        self._skip_cjk = tk.BooleanVar(value=bool(self.cfg.get("skip_chinese", True)))
        tk.Checkbutton(grid, text="跳过已经是中文的文本", variable=self._skip_cjk,
                       bg=PANEL, fg=FG, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=FG, bd=0, highlightthickness=0,
                       command=self._on_misc_change).grid(row=3, column=0, columnspan=2,
                                                          sticky="w", pady=(6, 0))

        # --- 右侧状态
        st = self._card(right, "实时状态", "")
        self._stat_labels: Dict[str, tk.Label] = {}
        # 两列排布，省一半高度，右侧才有空间给"最近译文"和"热键"
        for i, (key, label) in enumerate((("engine", "引擎"), ("fps", "刷新"),
                                          ("ocr", "OCR"), ("boxes", "识别"),
                                          ("sprites", "汉化"), ("pending", "待翻"))):
            r, c = divmod(i, 2)
            tk.Label(st, text=label, bg=PANEL, fg=MUTED, font=(FONT_UI, 9),
                     width=4, anchor="w").grid(row=r, column=c * 2, sticky="w",
                                               pady=1)
            v = tk.Label(st, text="—", bg=PANEL, fg=FG, font=(FONT_MONO, 9),
                         anchor="w")
            v.grid(row=r, column=c * 2 + 1, sticky="ew",
                   padx=(0, 12 if c == 0 else 0), pady=1)
            self._stat_labels[key] = v
        st.columnconfigure(1, weight=1)

        smp = self._card(right, "最近一条译文", "看到中文＝已经生效")
        self._sample_label = tk.Label(smp, text="还没有译文。点「试试看」先确认能识别到文字。",
                                      bg=PANEL, fg=MUTED, font=(FONT_UI, 9),
                                      justify="left", anchor="w", wraplength=244)
        self._sample_label.pack(fill="x")

        disp = self._card(right, "显示样式", "")
        self._mask_var = tk.BooleanVar(value=bool(self.cfg.get("style", {}).get("mask", True)))
        tk.Checkbutton(disp, text="用底色块盖住原文", variable=self._mask_var,
                       bg=PANEL, fg=FG, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=FG, bd=0, highlightthickness=0,
                       command=self._on_style_change).pack(anchor="w")
        self._top_var = tk.BooleanVar(value=bool(self.cfg.get("topmost", True)))
        tk.Checkbutton(disp, text="叠加层始终置顶", variable=self._top_var,
                       bg=PANEL, fg=FG, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=FG, bd=0, highlightthickness=0,
                       command=self._on_style_change).pack(anchor="w")
        tk.Label(disp, text="底色浓度（淡＝更能看清原画面）", bg=PANEL, fg=MUTED,
                 font=(FONT_UI, 9)).pack(anchor="w", pady=(8, 0))
        self._stipple_var = tk.StringVar(
            value={"": "实心", "gray50": "半透明", "gray25": "淡底"}.get(
                str(self.cfg.get("style", {}).get("stipple", "")), "实心"))
        self._stipple_combo = ttk.Combobox(
            disp, textvariable=self._stipple_var, state="readonly",
            values=["实心", "半透明", "淡底"], width=12)
        self._stipple_combo.pack(anchor="w", pady=(2, 0))
        self._stipple_combo.bind("<<ComboboxSelected>>",
                                 lambda e: self._on_style_change())

        tk.Label(disp, text="字号比例", bg=PANEL, fg=MUTED,
                 font=(FONT_UI, 9)).pack(anchor="w", pady=(8, 0))
        self._font_scale = tk.DoubleVar(
            value=float(self.cfg.get("style", {}).get("font_scale", 0.62)))
        ttk.Scale(disp, from_=0.42, to=0.92, orient="horizontal",
                  variable=self._font_scale,
                  command=lambda *_: self._on_style_change()).pack(fill="x")

        keys = self._card(right, "全局热键", "在游戏里直接按")
        for line in ("F8 选区开关 · F9 全屏开关 · F10 停止还原",
                     "F11 重新框选 · F12 显示/隐藏译文"):
            tk.Label(keys, text=line, bg=PANEL, fg=MUTED, font=(FONT_MONO, 9),
                     anchor="w").pack(fill="x")
        return page

    # ================================================================== 引擎页
    def _build_engine(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=BG)
        box = self._card(page, "翻译引擎", "可随时切换，立即生效")
        row = tk.Frame(box, bg=PANEL)
        row.pack(fill="x")
        tk.Label(row, text="当前引擎", bg=PANEL, fg=MUTED, font=(FONT_UI, 10),
                 width=10, anchor="w").pack(side="left")
        self._prov_var = tk.StringVar(value=PROVIDER_LABELS.get(
            self.cfg.get("provider", "litemt"), ""))
        self._prov_combo = ttk.Combobox(row, textvariable=self._prov_var, state="readonly",
                                        values=list(PROVIDER_LABELS.values()), width=44)
        self._prov_combo.pack(side="left")
        self._prov_combo.bind("<<ComboboxSelected>>", self._on_provider_change)

        self._hint_label = tk.Label(box, text="", bg=PANEL, fg=MUTED, font=(FONT_UI, 9),
                                    justify="left", anchor="w", wraplength=720)
        self._hint_label.pack(fill="x", pady=(10, 0))

        self._fields_frame = tk.Frame(box, bg=PANEL)
        self._fields_frame.pack(fill="x", pady=(10, 0))

        btns = tk.Frame(box, bg=PANEL)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="保存配置", style="Accent.TButton",
                   command=self._save_provider).pack(side="left")
        ttk.Button(btns, text="测试连接", command=self._test_provider).pack(side="left", padx=8)
        ttk.Button(btns, text="清空译文缓存", command=self._clear_trans_cache).pack(side="left")
        self._cache_label = tk.Label(btns, text="", bg=PANEL, fg=MUTED, font=(FONT_UI, 9))
        self._cache_label.pack(side="left", padx=10)

        tips = self._card(page, "怎么选", "")
        text = tk.Text(tips, height=9, bg=PANEL2, fg=FG, bd=0, wrap="word",
                       font=(FONT_UI, 9), insertbackground=FG, padx=12, pady=10)
        text.pack(fill="both", expand=True)
        text.insert("1.0", (
            "· 只想快点看懂菜单：留在 LiteMT 内置引擎，零配置、零延迟，完全离线。\n"
            "· 有大段剧情文本、追求通顺：接大模型（DeepSeek / 通义 / 硅基流动 / 自建 vLLM），"
            "配合缓存与批量请求，实际每秒能稳定翻十几条。\n"
            "· 完全离线但要有大模型质量：本机装 Ollama 拉一个 7B 中文模型，选「Ollama 本地大模型」。\n"
            "· 只是想验证一下：选「Google 翻译（免费）」，不用填任何密钥。\n\n"
            "所有译文都会写入本地缓存（translation_cache.json），同一句话只翻一次；"
            "术语表在译文产出后做替换，用来强制统一人名、技能名、道具名。"))
        text.configure(state="disabled")

        self._load_provider_fields(self.cfg.get("provider", "litemt"))
        self._refresh_cache_label()
        return page

    def _load_provider_fields(self, provider: str) -> None:
        for w in self._fields_frame.winfo_children():
            w.destroy()
        self._pw_vars.clear()
        self._provider = provider
        self._hint_label.configure(text=PROVIDER_HINTS.get(provider, ""))
        conf = self.cfg.provider_conf(provider)
        fields = PROVIDER_FIELDS.get(provider, [])
        if not fields:
            tk.Label(self._fields_frame, text="无需任何配置，开箱即用。", bg=PANEL, fg=OK,
                     font=(FONT_UI, 10)).pack(anchor="w", pady=6)
            return
        grid = tk.Frame(self._fields_frame, bg=PANEL)
        grid.pack(fill="x")
        for i, name in enumerate(fields):
            r, c = i % 4, (i // 4) * 2
            tk.Label(grid, text=FIELD_LABELS.get(name, name), bg=PANEL, fg=MUTED,
                     font=(FONT_UI, 9), anchor="w").grid(row=r, column=c, sticky="w",
                                                         padx=(0, 6), pady=5)
            var = tk.StringVar(value=str(conf.get(name, "")))
            show = "*" if name in ("api_key", "secret_key") else None
            entry = ttk.Entry(grid, textvariable=var, width=34, show=show or "")
            entry.grid(row=r, column=c + 1, sticky="ew", padx=(0, 18), pady=5)
            self._pw_vars[name] = var
        for c in (1, 3):
            grid.columnconfigure(c, weight=1)

    def _on_provider_change(self, _e=None) -> None:
        label = self._prov_var.get()
        for key, lab in PROVIDER_LABELS.items():
            if lab == label:
                self.cfg.set("provider", key)
                self.service.invalidate()
                self._load_provider_fields(key)
                self.cfg.save()
                self.log(f"翻译引擎切换为：{lab}")
                return

    def _collect_provider(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for name, var in self._pw_vars.items():
            out[name] = var.get().strip()
        return out

    def _save_provider(self) -> None:
        if self._pw_vars:
            self.cfg.update_provider(self._provider, self._collect_provider())
            self.service.invalidate()
            self.log("引擎配置已保存")
            self._status_note.set("引擎配置已保存")

    def _test_provider(self) -> None:
        self._save_provider()

        def work() -> None:
            try:
                msg = self.service.test()
                self.log("自检通过：" + msg)
            except Exception as exc:
                self.log(f"自检失败：{exc}")

        threading.Thread(target=work, daemon=True).start()
        self._status_note.set("正在测试引擎…")

    def _clear_trans_cache(self) -> None:
        n = self.service.clear_cache()
        self.pipeline.reset_cache()
        self._refresh_cache_label()
        self.log(f"已清空译文缓存（{n} 条）")

    def _refresh_cache_label(self) -> None:
        try:
            self._cache_label.configure(text=f"缓存 {self.service.cache_size()} 条")
        except Exception:
            pass

    # ================================================================== 术语表
    def _build_glossary(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=BG)
        box = self._card(page, "术语表", "译文产出后按此表替换，保证名词统一")
        row = tk.Frame(box, bg=PANEL)
        row.pack(fill="x")
        tk.Label(row, text="原文", bg=PANEL, fg=MUTED, font=(FONT_UI, 9)).pack(side="left")
        self._gl_src = tk.StringVar()
        ttk.Entry(row, textvariable=self._gl_src, width=22).pack(side="left", padx=6)
        tk.Label(row, text="译名", bg=PANEL, fg=MUTED, font=(FONT_UI, 9)).pack(side="left")
        self._gl_dst = tk.StringVar()
        ttk.Entry(row, textvariable=self._gl_dst, width=22).pack(side="left", padx=6)
        ttk.Button(row, text="添加", command=self._glossary_add).pack(side="left")
        ttk.Button(row, text="删除选中",
                   command=self._glossary_del).pack(side="left", padx=6)

        wrap = tk.Frame(box, bg=PANEL)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        cols = ("src", "dst")
        self._gl_tree = ttk.Treeview(wrap, columns=cols, show="headings", height=12)
        self._gl_tree.heading("src", text="原文 / 匹配模式")
        self._gl_tree.heading("dst", text="译名")
        self._gl_tree.column("src", width=320)
        self._gl_tree.column("dst", width=320)
        self._gl_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self._gl_tree.yview)
        sb.pack(side="left", fill="y")
        self._gl_tree.configure(yscrollcommand=sb.set)
        self._reload_glossary()
        tk.Label(box, text="匹配不区分大小写，按字面替换；适合人名、技能名、道具名。",
                 bg=PANEL, fg=MUTED, font=(FONT_UI, 9), anchor="w").pack(fill="x", pady=(8, 0))
        return page

    def _reload_glossary(self) -> None:
        for i in self._gl_tree.get_children():
            self._gl_tree.delete(i)
        for k, v in (self.cfg.get("glossary") or {}).items():
            self._gl_tree.insert("", "end", values=(k, v))

    def _glossary_add(self) -> None:
        src, dst = self._gl_src.get().strip(), self._gl_dst.get().strip()
        if not src or not dst:
            return
        g = dict(self.cfg.get("glossary") or {})
        g[src] = dst
        self.cfg.set("glossary", g)
        self.cfg.save()
        self._gl_src.set("")
        self._gl_dst.set("")
        self._reload_glossary()
        self.log(f"术语表新增：{src} → {dst}")

    def _glossary_del(self) -> None:
        for item in self._gl_tree.selection():
            src = self._gl_tree.item(item, "values")[0]
            g = dict(self.cfg.get("glossary") or {})
            g.pop(src, None)
            self.cfg.set("glossary", g)
        self.cfg.save()
        self._reload_glossary()

    # ================================================================== 日志/帮助
    def _build_log(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=BG)
        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="清空日志", command=lambda: self._log_text_delete()).pack(side="left")
        ttk.Button(bar, text="打开数据目录",
                   command=lambda: webbrowser.open(str(app_dir()))).pack(side="left", padx=8)
        box = Card(page)
        box.pack(fill="both", expand=True)
        self._log_text = tk.Text(box, bg="#0b0d10", fg="#c8d1dd", bd=0,
                                 font=(FONT_MONO, 9), wrap="word", padx=12, pady=10,
                                 insertbackground=FG, state="disabled")
        self._log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=self._log_text.yview)
        sb.pack(side="right", fill="y")
        self._log_text.configure(yscrollcommand=sb.set)
        return page

    def _append_log(self, line: str) -> None:
        if not hasattr(self, "_log_text"):
            return
        self._log_text.configure(state="normal")
        self._log_text.insert("end", line + "\n")
        if int(self._log_text.index("end-1c").split(".")[0]) > 800:
            self._log_text.delete("1.0", "120.0")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _log_text_delete(self) -> None:
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _build_help(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=BG)
        box = Card(page)
        box.pack(fill="both", expand=True)
        text = tk.Text(box, bg=PANEL, fg=FG, bd=0, wrap="word", padx=20, pady=18,
                       font=(FONT_UI, 10), insertbackground=FG)
        text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        sb.pack(side="right", fill="y")
        text.configure(yscrollcommand=sb.set)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")
        return page

    # ================================================================== 模式与运行
    def _select_mode(self, mode: str) -> None:
        self._mode_var.set(mode)
        self._on_mode_change()

    def _on_mode_change(self) -> None:
        mode = self._mode_var.get()
        for key, card in getattr(self, "_mode_cards", {}).items():
            card.configure(highlightbackground=ACCENT if key == mode else BORDER,
                           highlightthickness=2 if key == mode else 1)
        self.cfg.set("mode", mode)
        self.cfg.save()
        self._run_hint.configure(text=self._hint_for(mode))

    def _hint_for(self, mode: str) -> str:
        if mode == "region":
            return f"当前区域：{self._region_text()}　（F11 可重新框选）"
        if mode == "fullscreen":
            return "整屏识别；文字量大时建议把刷新间隔调到 600ms 以上。"
        return "注入模式会改写游戏内存，建议先用演练模式确认能定位到文本。"

    def _region_text(self) -> str:
        x, y, w, h = (self.cfg.get("region") or [0, 0, 0, 0])[:4]
        hwnd = int(self.cfg.get("region_hwnd", 0) or 0)
        tail = "　[跟随窗口]" if hwnd and winapi.is_window(hwnd) else ""
        return f"x={x}  y={y}  {w}×{h}{tail}"

    def _pick_region(self) -> None:
        if self.selector is not None:
            try:
                self.selector._close()
            except Exception:
                pass
            self.selector = None
        self.selector = RegionSelector(self, self._on_region_picked, self._on_region_cancel)
        self.selector.win.lift()
        self._status_note.set("拖动框选区域…")

    def _on_region_picked(self, bbox) -> None:
        self.selector = None
        self.cfg.set("region", list(bbox))
        self.cfg.set("region_hwnd", 0)
        self.cfg.save()
        self._region_label.configure(text=self._region_text())
        self.log(f"已设置汉化区域：{bbox}")
        self._status_note.set("区域已更新")
        self._select_mode("region")
        if self.pipeline.running and self.pipeline.mode == "region":
            self._start()

    def _on_region_cancel(self) -> None:
        self.selector = None
        self._status_note.set("已取消框选")

    def _pick_fullscreen(self) -> None:
        self.cfg.set("region", list(winapi.virtual_screen_rect()))
        self.cfg.set("region_hwnd", 0)
        self.cfg.save()
        self._region_label.configure(text=self._region_text())
        self._select_mode("fullscreen")

    def _lock_window(self) -> None:
        """把「汉化区域」直接锁到某个窗口的画面范围，省掉手动画框。"""
        w: Optional[winapi.WindowInfo] = None
        idx = self._proc_combo.current()
        if 0 <= idx < len(self._windows):
            w = self._windows[idx]
        if w is None:
            fg = winapi.foreground_hwnd()
            for cand in self._windows:
                if cand.hwnd == fg:
                    w = cand
                    break
        if w is None:
            self._refresh_windows()
            messagebox.showinfo(
                "还没有可选窗口",
                "先把游戏窗口切到前台，或在上面的下拉框里选一个窗口，再点「锁定游戏窗口」。")
            return
        rect = winapi.client_rect_on_screen(w.hwnd) or w.rect
        self.cfg.set("region", list(rect))
        self.cfg.set("region_hwnd", int(w.hwnd))
        inj = dict(self.cfg.get("inject") or {})
        inj["pid"], inj["hwnd"] = w.pid, w.hwnd
        self.cfg.set("inject", inj)
        self.cfg.save()
        self._region_label.configure(text=self._region_text())
        self._select_mode("region")
        self.log(f"汉化区域已锁定到「{w.title}」：{rect}")
        self._status_note.set("区域已锁定到游戏窗口，点「试试看」验证")

    def _probe_region(self) -> None:
        """跑一小段真实流水线，把「识别到什么 / 翻成什么」摆给用户看。"""
        if self._probe_busy:
            return
        mode = "fullscreen" if self._mode_var.get() == "fullscreen" else "region"
        if mode == "region":
            b = self.cfg.get("region") or [0, 0, 0, 0]
            if b[2] < 16 or b[3] < 16:
                messagebox.showinfo("还没有区域",
                                    "先点「锁定游戏窗口」，或「框选区域」框一块要汉化的地方。")
                return
        self._probe_busy = True
        self._probe_started_here = not self.pipeline.running
        self._btn_probe.configure(state="disabled")
        self._status_note.set("正在取一帧试翻，请稍等…")
        if self._probe_started_here:
            self._select_mode(mode)
            self._start()
        self._probe_deadline = time.time() + 8.0
        self.after(400, self._poll_probe)

    def _poll_probe(self) -> None:
        sprites = self.overlay._last_payload or []
        known = dict(self.pipeline._known)
        if time.time() < self._probe_deadline and not sprites:
            self.after(350, self._poll_probe)
            return
        self._probe_busy = False
        try:
            self._btn_probe.configure(state="normal")
        except Exception:
            pass
        pairs = list(known.items())[:8]
        if self._probe_started_here:
            self._stop()
        if sprites:
            body = "\n".join(f"· {s}\n  → {d}" for s, d in pairs) or "（译文还在生成，稍等一秒再看）"
            messagebox.showinfo(
                "试翻结果：汉化已生效",
                f"这一帧识别到 {len(sprites)} 条可汉化文字，译文已经画在游戏画面上。\n\n"
                f"{body}\n\n"
                "确认没问题就点「开始汉化」持续运行；位置或字号不合适，可调右侧「显示样式」。")
            self._status_note.set(f"试翻成功：{len(sprites)} 条")
        else:
            tip = ("没识别到文字。常见原因：\n"
                   "· 区域里没有文字，或「锁定游戏窗口」后游戏又被拖动了；\n"
                   "· 游戏是独占全屏，叠加层无法显示（改成窗口化/无边框全屏）；\n"
                   "· 该语言没有对应 OCR 语言包 —— 在「识别与刷新」里换一个 OCR 引擎再试。")
            messagebox.showwarning("试翻结果：没有识别到文字", tip)
            self._status_note.set("试翻失败：没有识别到文字")

    def _on_interval(self) -> None:
        ms = int(self._interval_var.get())
        self._interval_label.configure(text=f"{ms} ms")
        self.cfg.set("interval_ms", ms)
        self.cfg.save()

    def _on_ocr_change(self, labels: Dict[str, str]) -> None:
        label = self._ocr_combo.get()
        for key, lab in labels.items():
            if lab == label:
                self.cfg.set("ocr_engine", key)
                self.pipeline._engine = None
                self.cfg.save()
                self.log(f"OCR 引擎设置为 {label}（下次开始生效）")

    def _on_lang_change(self, labels: Dict[str, str]) -> None:
        label = self._lang_combo.get()
        for key, lab in labels.items():
            if lab == label:
                self.cfg.set("source_lang", key)
                self.pipeline._engine = None        # OCR 模型要按语言换
                self.service.invalidate()           # 翻译引擎也要换（中→日）
                self.cfg.save()
                self.log(f"游戏语言设置为 {label}（下次开始生效）")

    def _on_misc_change(self) -> None:
        self.cfg.set("skip_chinese", bool(self._skip_cjk.get()))
        self.cfg.save()

    def _on_style_change(self) -> None:
        st = dict(self.cfg.get("style") or {})
        st["mask"] = bool(self._mask_var.get())
        st["font_scale"] = round(float(self._font_scale.get()), 2)
        st["stipple"] = {"实心": "", "半透明": "gray50",
                         "淡底": "gray25"}.get(self._stipple_var.get(), "")
        self.cfg.set("style", st)
        self.cfg.set("topmost", bool(self._top_var.get()))
        self.cfg.save()
        self.overlay.mask = st["mask"]
        self.overlay.font_scale = st["font_scale"]
        self.overlay.stipple = st["stipple"]
        self.overlay._fonts.clear()
        self.overlay.redraw()

    def _on_dry_toggle(self) -> None:
        inj = dict(self.cfg.get("inject") or {})
        inj["dry_run"] = bool(self._dry_var.get())
        self.cfg.set("inject", inj)
        self.cfg.save()

    def _refresh_windows(self) -> None:
        self._windows = winapi.list_windows()
        values = [w.label for w in self._windows]
        self._proc_combo.configure(values=values)
        if not values:
            self._proc_var.set("（未找到可注入窗口）")
            return
        cur_pid = int(self.cfg.get("inject", {}).get("pid", 0))
        for i, w in enumerate(self._windows):
            if w.pid == cur_pid:
                self._proc_combo.current(i)
                return
        self.log(f"已列出 {len(values)} 个可注入窗口")

    def _on_proc_pick(self, _e=None) -> None:
        idx = self._proc_combo.current()
        if idx < 0 or idx >= len(self._windows):
            return
        w = self._windows[idx]
        inj = dict(self.cfg.get("inject") or {})
        inj["pid"] = w.pid
        inj["hwnd"] = w.hwnd
        self.cfg.set("inject", inj)
        self.cfg.save()
        self.log(f"注入目标：{w.title}（PID {w.pid}）")

    # ================================================================== 启动/停止
    def _start(self) -> None:
        mode = self._mode_var.get()
        if mode == "region":
            b = self.cfg.get("region") or [0, 0, 0, 0]
            if b[2] < 16 or b[3] < 16:
                messagebox.showinfo("还没有选区", "请先点「框选区域」框出一块要汉化的区域。")
                return
            bbox = tuple(int(v) for v in b[:4])
            hwnd = 0
        elif mode == "fullscreen":
            bbox = winapi.virtual_screen_rect()
            hwnd = 0
        else:
            inj = self.cfg.get("inject") or {}
            pid = int(inj.get("pid", 0))
            if not pid:
                messagebox.showinfo("还没有目标", "请在上方下拉框里选择要注入的游戏窗口，再点刷新。")
                return
            hwnd = int(inj.get("hwnd", 0))
            if not hwnd or not winapi.is_window(hwnd):
                messagebox.showwarning("目标窗口已关闭", "选中的游戏窗口已经不在了，请刷新后重选。")
                self._refresh_windows()
                return
            if not inj.get("dry_run", True) and not self.cfg.get("warn_accepted", False):
                ok = messagebox.askokcancel(
                    "注入模式风险提示",
                    "注入模式会直接改写游戏进程内存里的文本。\n\n"
                    "· 可能造成游戏崩溃或存档异常，请先备份存档；\n"
                    "· 建议先用「演练模式」确认能够定位到文本；\n"
                    "· 停止汉化时会自动把原文写回去。\n\n"
                    "确认要继续吗？")
                if not ok:
                    return
                self.cfg.set("warn_accepted", True)
                self.cfg.save()
            r = winapi.client_rect_on_screen(hwnd)
            bbox = r or winapi.window_rect(hwnd) or (0, 0, 0, 0)

        self.overlay.set_geometry(bbox)
        self.overlay.show()
        try:
            self.pipeline.start(mode, bbox, hwnd)
        except Exception as exc:
            self.overlay.hide()
            self.log(f"启动失败：{exc}")
            messagebox.showerror("启动失败", str(exc))
            return
        self._btn_start.configure(state="disabled")
        self._run_hint.configure(text="正在汉化…按 F10 或点「停止并还原」结束")
        self.after(700, self._check_overlay_visible)

    def _check_overlay_visible(self) -> None:
        if not self.pipeline.running or not self.overlay.visible:
            return
        hwnd = winapi.hwnd_of(self.overlay.win)
        if not hwnd or not winapi.user32.IsWindowVisible(hwnd) or \
                winapi.user32.IsIconic(hwnd):
            self.overlay.show()
            winapi.keep_topmost(hwnd, True)
            self.log("叠加层被系统隐藏了，已重新置顶；若仍看不到，请把游戏改成窗口化。")

    def _stop(self) -> None:
        # 先把汉化层撤掉，界面立刻恢复原样；还原内存/收线程放到后台做，
        # 免得玩家按 F10 后还要盯着旧译文卡一两秒。
        self.overlay.clear()
        self.overlay.hide()
        self._btn_start.configure(state="normal")
        self._run_hint.configure(text=self._hint_for(self._mode_var.get()))
        self._status_note.set("正在停止并还原…")
        if self._stopping:
            return
        self._stopping = True

        def work() -> None:
            try:
                self.pipeline.stop()
            finally:
                self.ui_queue.put(("call", self._after_stop))

        threading.Thread(target=work, daemon=True, name="gamelens-stop").start()

    def _after_stop(self) -> None:
        self._stopping = False
        self._status_note.set("已停止")
        self._sample_label.configure(text="已停止。再点「开始汉化」重新运行。", fg=MUTED)

    def _reset_memory(self) -> None:
        self.pipeline.reset_cache()
        self.log("已清空本次运行的译文记忆，下一帧会重新翻译")
        self._status_note.set("译文记忆已清空")

    # ================================================================== 回调
    def _on_frame(self, bbox, sprites: List[Sprite]) -> None:
        self.ui_queue.put(("frame", (bbox, sprites)))

    def _apply_frame(self, bbox, sprites: List[Sprite]) -> None:
        # 已在停止流程里：丢弃排队中的旧帧，否则会把刚撤掉的汉化层又画回来
        if self._stopping or not self.pipeline.running:
            return
        if bbox != self.overlay.geometry:
            self.overlay.set_geometry(bbox)
        if not self.overlay.visible:
            self.overlay.show()
        self.overlay.render(sprites)

    def _on_status(self, st: Status) -> None:
        self.ui_queue.put(("status", st))

    def _apply_status(self, st: Status) -> None:
        if not st.running and not self.pipeline.running:
            try:
                self._btn_start.configure(state="normal")
            except Exception:
                pass
        self._stat_labels["engine"].configure(text=st.engine or "—")
        self._stat_labels["fps"].configure(text=f"{st.fps:.1f} fps")
        self._stat_labels["ocr"].configure(text=f"{st.ocr_ms} ms")
        self._stat_labels["boxes"].configure(text=str(st.boxes))
        self._stat_labels["sprites"].configure(text=str(st.sprites))
        self._stat_labels["pending"].configure(text=str(st.pending))
        if st.sample:
            self._sample_label.configure(text=st.sample, fg=OK)
        if st.note:
            self._status_note.set(st.note)

    # ================================================================== 热键
    def _register_hotkeys(self) -> None:
        self.hotkeys = HotkeyManager()
        self.hotkeys.register("F8", lambda: self._hotkey(self._toggle_region))
        self.hotkeys.register("F9", lambda: self._hotkey(self._toggle_fullscreen))
        self.hotkeys.register("F10", lambda: self._hotkey(self._stop))
        self.hotkeys.register("F11", lambda: self._hotkey(self._pick_region))
        self.hotkeys.register("F12", lambda: self._hotkey(self._toggle_overlay))
        self.hotkeys.start()
        if self.hotkeys.failed:
            self.log(f"热键注册失败（可能被其他软件占用）：{self.hotkeys.failed}")

    def _hotkey(self, fn) -> None:
        self.ui_queue.put(("call", fn))

    def _toggle_region(self) -> None:
        if self.pipeline.running and self.pipeline.mode == "region":
            self._stop()
        else:
            self._select_mode("region")
            self._start()

    def _toggle_fullscreen(self) -> None:
        if self.pipeline.running and self.pipeline.mode == "fullscreen":
            self._stop()
        else:
            self._select_mode("fullscreen")
            self._start()

    def _toggle_overlay(self) -> None:
        if self.overlay.visible:
            self.overlay.hide()
        else:
            self.overlay.show()
            self.overlay.redraw()

    # ================================================================== 关闭
    def _on_close(self) -> None:
        """关窗即彻底退出：还原注入、停线程、存配置，最后强制结束进程。

        少了最后这一步，窗口关了但进程还在（占用 exe，下次没法覆盖升级）。
        """
        for step in (lambda: self.pipeline.stop(quiet=True),
                     lambda: self.hotkeys.stop(),
                     lambda: self.cfg.save(),
                     lambda: self.service.flush_cache(),
                     lambda: self.overlay.destroy(),
                     lambda: self.destroy()):
            try:
                step()
            except Exception:
                pass
        os._exit(0)


HELP_TEXT = """GameLens 使用说明

零、三步上手（最快路径）

   1) 在「注入目标进程」下拉框里选中你的游戏窗口（游戏先切到窗口化/无边框全屏）；
   2) 点「锁定游戏窗口」——汉化区域自动对齐到游戏画面，之后游戏窗口挪动也会跟着走；
   3) 点「试试看」：工具会真实跑一帧，把「识别到什么 → 翻成什么」列给你看。
      看到中文对照没问题，再点「开始汉化」持续运行。

   「试试看」是专门用来确认"汉化到底生效没有"的：它不做假动作，
   走的就是正式运行的截图→OCR→翻译→上屏全流程，跑 5 秒自动停。

一、三种模式怎么用

1) 屏幕选区汉化（最常用）
   · 点「锁定游戏窗口」自动取景，或点「框选区域」手动拖一块（对话框、任务栏、字幕条都行）；
   · 点「开始汉化」，或直接按 F8。
   · 汉化层是「点击穿透」的独立窗口：不抢焦点、不吞按键、不拦截鼠标。
     你在原按钮位置点一下，游戏照样收到点击，所以可以一边看中文一边继续玩。

2) 全屏汉化
   · 按 F9 或点「整屏」，整块屏幕都做识别与翻译。
   · 全屏模式会把叠加层临时藏起来再截图（约 20ms），避免 OCR 读到自己写的字。

3) 内部注入式汉化
   · 下拉框选择游戏窗口 → 点「开始汉化」（建议先勾「演练模式」确认能定位到文本）。
   · 工作方式：遍历游戏进程里可写的内存区，按 UTF-16LE / UTF-8 / GBK
     搜索屏幕上的原文字节，命中后把译文写回同一块缓冲区
     （UTF-16 按缓冲区容量写、单字节按原文字节数写，绝不越界），
     游戏下一帧自己就把中文渲染出来了。
   · 停止时会自动把所有写入过的位置还原成原文。
   · 如果游戏把文字烘焙进贴图（GPU 纹理），内存里没有可写的字符串，
     这时会退回叠加层显示译文（可在日志中查看原因）。

二、快捷键
   F8   开/关选区汉化        F9   开/关全屏汉化
   F10  停止并还原            F11  重新框选
   F12  显示/隐藏译文层

三、翻译引擎
   · LiteMT 内置离线引擎：词表 + 句型模板 + 词形还原 + 时态构造，零依赖、完全离线、
     单条 <1ms，适合菜单/按钮/短提示这类界面文字。
   · 大模型 / 翻译 API：OpenAI 兼容协议（DeepSeek、通义、硅基流动、自建 vLLM、
     LM Studio、One-API 中转）、Ollama 本机模型、Google 免费端点、DeepL、百度。
     切换后立即生效，同一句话只会请求一次（本地缓存）。
   · 术语表：译完后按表替换，用来钉死人名、技能名、道具名。

四、识别与刷新
   · OCR 引擎「自动」的规则：装了英文/日文/韩文语言包 → 用系统 OCR（约 80ms）；
     只有中文包 → 用内置 RapidOCR 离线模型（慢一些，但读英文界面更准）。
     系统 OCR 连续几帧认不出字时会自动切到离线模型。
   · 刷新间隔：默认 450ms。文字多、性能吃紧就调到 600~800ms。
   · 只对"发生变化的区域"重新识别，静止界面几乎不占 CPU。

五、显示样式
   · 用底色块盖住原文：关掉就只画字，留着原画透出来。
   · 底色浓度：实心 / 半透明 / 淡底，越淡越能看清原画面。
   · 字号比例：统一按画面中等行高换算，避免大小不一。
   · 叠加层始终置顶：需要把游戏压回去时临时关掉。

六、常见问题
   · 叠加层不显示？独占全屏（Exclusive Fullscreen）的游戏会独占画面，
     请把游戏改成「窗口化」或「无边框全屏」。
   · 中文没出现？点「试试看」看是识别不到还是翻不出来：
     识别 0 条 → 区域不对或 OCR 引擎不合适；识别到但没译文 → 换翻译引擎。
   · 文字位置偏了？多半是 DPI 缩放：本工具已声明 Per-Monitor V2 感知，
     若仍有偏移，把游戏和本工具都设成同一个缩放比例。
   · 注入模式崩溃？先在演练模式确认；只对确实在内存里找得到的文本启用写入。

七、数据目录
   配置、译文缓存都在 exe 同级的 GameLensData 目录（不可写时落到 %APPDATA%\\GameLens）。
"""


def run() -> None:
    app = App()
    app.mainloop()
