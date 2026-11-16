"""一个假的游戏窗口，用来验证 GameLens 的三种模式。

它故意把界面文字放在 ctypes 申请的 UTF-16 宽字符缓冲区里，
并且每 120ms 从缓冲区重新读一次来刷新界面——这样"内部注入式汉化"
真的能改写缓冲区，你就能在窗口里看到文字变成中文。

运行：python tools/fake_game.py
"""
from __future__ import annotations

import ctypes
import tkinter as tk

BG = "#10131a"
PANEL = "#1a1f2b"
GOLD = "#d8b25a"
TXT = "#e8e2d4"
DIM = "#8a8377"


class Buf:
    """一块 512 字符的 UTF-16 缓冲区，模拟游戏里的文本对象。"""

    def __init__(self, text: str) -> None:
        self._buf = ctypes.create_unicode_buffer(512)
        self._buf.value = text

    @property
    def addr(self) -> int:
        return ctypes.addressof(self._buf)

    def get(self) -> str:
        return self._buf.value


class FakeGame(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Fake Quest - Chapter I")
        self.geometry("960x560+120+90")
        self.configure(bg=BG)
        self.clicks = 0
        self.clicklog = ""

        self.b_title = Buf("ANCIENT CAVE OF DESPAIR")
        self.b_sub = Buf("Chapter I : The Broken Seal")
        self.b_dialog = Buf("Welcome, hero. The dragon has taken the crystal. Will you help us?")
        self.b_quest = Buf("Press Tab to open the map")
        self.b_hp = Buf("Health 120/200    Mana 45/80")
        self.b_status = Buf("No action yet.")
        self.buttons = [
            ("New Game", Buf("New Game")),
            ("Continue", Buf("Continue")),
            ("Settings", Buf("Settings")),
            ("Quit Game", Buf("Quit Game")),
        ]

        self._build()
        self.after(120, self._refresh)

    def _build(self) -> None:
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=36, pady=(30, 0))
        self.lbl_title = tk.Label(head, text="", bg=BG, fg=GOLD,
                                  font=("Georgia", 26, "bold"))
        self.lbl_title.pack(anchor="w")
        self.lbl_sub = tk.Label(head, text="", bg=BG, fg=DIM, font=("Georgia", 13))
        self.lbl_sub.pack(anchor="w", pady=(4, 0))

        hr = tk.Frame(self, bg="#2a3140", height=1)
        hr.pack(fill="x", padx=36, pady=18)

        mid = tk.Frame(self, bg=BG)
        mid.pack(fill="both", expand=True, padx=36)
        left = tk.Frame(mid, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(mid, bg=PANEL, width=330, height=250)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        # 对话框
        dialog = tk.Frame(left, bg=PANEL, highlightbackground="#2f3746", highlightthickness=1)
        dialog.pack(fill="x")
        tk.Label(dialog, text="◆  VILLAGE ELDER", bg=PANEL, fg=GOLD,
                 font=("Consolas", 10, "bold")).pack(anchor="w", padx=16, pady=(12, 4))
        self.lbl_dialog = tk.Label(dialog, text="", bg=PANEL, fg=TXT, justify="left",
                                   anchor="w", wraplength=430, font=("Georgia", 14))
        self.lbl_dialog.pack(anchor="w", padx=16, pady=(0, 14))

        # 状态条
        self.lbl_hp = tk.Label(left, text="", bg=BG, fg="#9fd3a8",
                               font=("Consolas", 12))
        self.lbl_hp.pack(anchor="w", pady=(16, 0))
        self.lbl_quest = tk.Label(left, text="", bg=BG, fg="#7fb2e5",
                                  font=("Georgia", 12))
        self.lbl_quest.pack(anchor="w", pady=(8, 0))
        self.lbl_status = tk.Label(left, text="", bg=BG, fg=DIM, font=("Consolas", 11))
        self.lbl_status.pack(anchor="w", pady=(14, 0))

        # 按钮列
        for name, buf in self.buttons:
            b = tk.Button(right, text=buf.get(), bg="#232b3a", fg=TXT, bd=0,
                          activebackground="#2f3a4d", activeforeground=GOLD,
                          font=("Georgia", 13), cursor="hand2",
                          command=lambda n=name: self._click(n))
            b.pack(fill="x", padx=18, pady=(12 if name == "New Game" else 6, 0),
                   ipady=8)
            buf.__dict__["_btn"] = b

        tk.Label(self, text="Fake Quest · 用于验证 GameLens 的汉化与点击穿透",
                 bg=BG, fg="#4d5566", font=("Consolas", 9)).pack(side="bottom", pady=10)

    def _click(self, name: str) -> None:
        self.clicks += 1
        self.b_status.value = f"Clicked: {name}  (#{self.clicks})"
        # 供自动化验证：把点击事件追加到日志文件，证明"点击穿透"真的生效
        if self.clicklog:
            try:
                with open(self.clicklog, "a", encoding="utf-8") as f:
                    f.write(f"{name}\t{self.clicks}\n")
            except OSError:
                pass

    def _refresh(self) -> None:
        self.lbl_title.configure(text=self.b_title.get())
        self.lbl_sub.configure(text=self.b_sub.get())
        self.lbl_dialog.configure(text=self.b_dialog.get())
        self.lbl_hp.configure(text=self.b_hp.get())
        self.lbl_quest.configure(text=self.b_quest.get())
        self.lbl_status.configure(text=self.b_status.get())
        for _name, buf in self.buttons:
            btn = buf.__dict__.get("_btn")
            if btn is not None and btn.cget("text") != buf.get():
                btn.configure(text=buf.get())
        self.after(120, self._refresh)


if __name__ == "__main__":
    import json
    import sys

    app = FakeGame()
    if "--topmost" in sys.argv:
        app.attributes("-topmost", True)
    if "--pos" in sys.argv:
        i = sys.argv.index("--pos")
        x, y = sys.argv[i + 1], sys.argv[i + 2]
        app.geometry(f"960x560+{x}+{y}")
    if "--clicklog" in sys.argv:
        app.clicklog = sys.argv[sys.argv.index("--clicklog") + 1]
    app.update()
    info = {
        "pid": __import__("os").getpid(),
        "hwnd": int(app.winfo_id()),
        "rect": [app.winfo_rootx(), app.winfo_rooty(),
                 app.winfo_width(), app.winfo_height()],
        "buffers": {name: buf.get() for name, buf in
                    [("title", app.b_title), ("sub", app.b_sub),
                     ("dialog", app.b_dialog), ("quest", app.b_quest),
                     ("hp", app.b_hp), ("status", app.b_status)]},
    }
    print("FAKEGAME_READY " + json.dumps(info, ensure_ascii=False), flush=True)
    app.mainloop()
