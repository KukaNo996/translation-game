"""统一的深色视觉主题。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

BG = "#0e1013"
PANEL = "#15181d"
PANEL2 = "#1b1f26"
BORDER = "#272c35"
FG = "#e6e8eb"
MUTED = "#8b93a1"
ACCENT = "#4c8dff"
ACCENT_DIM = "#2f5fbf"
OK = "#3ddc97"
WARN = "#f5a524"
ERR = "#ff5c5c"

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

RADIUS = 10


def apply_theme(root: tk.Misc) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL2,
                    bordercolor=BORDER, lightcolor=PANEL2, darkcolor=PANEL2,
                    font=(FONT_UI, 10))
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("TLabel", background=BG, foreground=FG, font=(FONT_UI, 10))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(FONT_UI, 9))
    style.configure("Panel.TLabel", background=PANEL, foreground=FG)
    style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED,
                    font=(FONT_UI, 9))
    style.configure("H1.TLabel", background=BG, foreground=FG, font=(FONT_UI, 17, "bold"))
    style.configure("H2.TLabel", background=PANEL, foreground=FG, font=(FONT_UI, 11, "bold"))

    style.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0,
                    focusthickness=0, padding=(12, 7), font=(FONT_UI, 10))
    style.map("TButton",
              background=[("active", "#242a33"), ("pressed", "#2c333e"),
                          ("disabled", "#191d23")],
              foreground=[("disabled", "#5b6270")])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    font=(FONT_UI, 10, "bold"), padding=(14, 8))
    style.map("Accent.TButton",
              background=[("active", "#5f9bff"), ("pressed", ACCENT_DIM),
                          ("disabled", "#26364f")],
              foreground=[("disabled", "#8fa3c4")])
    style.configure("Ghost.TButton", background=PANEL, foreground=FG, padding=(10, 6))
    style.map("Ghost.TButton", background=[("active", "#20252d")])

    style.configure("TCheckbutton", background=PANEL, foreground=FG,
                    font=(FONT_UI, 10), focuscolor=PANEL)
    style.map("TCheckbutton", background=[("active", PANEL)],
              indicatorcolor=[("selected", ACCENT), ("!selected", PANEL2)])
    style.configure("TRadiobutton", background=PANEL, foreground=FG, font=(FONT_UI, 10))
    style.map("TRadiobutton", background=[("active", PANEL)],
              indicatorcolor=[("selected", ACCENT), ("!selected", PANEL2)])

    style.configure("TEntry", fieldbackground=PANEL2, foreground=FG,
                    insertcolor=FG, bordercolor=BORDER, padding=6)
    style.map("TEntry", fieldbackground=[("focus", "#20252d")])
    style.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                    foreground=FG, arrowcolor=MUTED, bordercolor=BORDER, padding=5)
    style.map("TCombobox",
              fieldbackground=[("readonly", PANEL2)],
              selectbackground=[("readonly", PANEL2)],
              selectforeground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", PANEL2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    style.configure("TScale", background=PANEL, troughcolor=PANEL2,
                    bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=BG,
                    bordercolor=BG, arrowcolor=MUTED, width=10)
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=FG, bordercolor=BORDER, rowheight=26,
                    font=(FONT_UI, 10))
    style.configure("Treeview.Heading", background=PANEL2, foreground=MUTED,
                    borderwidth=0, font=(FONT_UI, 9))
    style.map("Treeview", background=[("selected", ACCENT_DIM)],
              foreground=[("selected", "#ffffff")])
    style.configure("TSeparator", background=BORDER)


class Card(tk.Frame):
    """带描边的卡片容器。"""

    def __init__(self, master, **kw):
        kw.setdefault("bg", PANEL)
        kw.setdefault("highlightbackground", BORDER)
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("bd", 0)
        super().__init__(master, **kw)
