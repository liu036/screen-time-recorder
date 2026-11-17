# -*- coding: utf-8 -*-
"""番茄钟提醒小窗（独立进程）。

为什么独立进程：主进程主线程被 win32gui.PumpMessages() 占用，跑不了 Tk 事件
循环；待办窗口也只负责列表。这里用一次性进程弹一个右下角置顶小窗，点完按钮
就退出，不常驻。

用法（由主进程拉起，一般不用手敲）：
    pythonw -m screen_time_recorder.pomodoro_popup --kind focus_done
    pythonw -m screen_time_recorder.pomodoro_popup --kind break_done

kind:
- focus_done：专注结束，展示时长/专注率，提供「休息并继续 / 直接下一轮 / 结束」
- break_done：休息结束（下一轮已自动开始），5 秒后自动消失
"""
import ctypes
import json
import os
import sys
import urllib.request

import tkinter as tk
from tkinter import font as tkfont

from . import config

API_BASE = os.environ.get("STR_API", "http://127.0.0.1:8765")

# 与待办窗口同一套 palm 语义色
BG = "#F5F2EA"
CARD = "#FFFDF8"
INK = "#2B2B28"
MUTED = "#8A8578"
FAINT = "#E2DCCC"
LINE = "#EAE4D5"
ACCENT = "#D4A017"
ACCENT_DARK = "#B5650F"
DANGER = "#C0553B"

WIDTH = 380
HEIGHT = 200

_MUTEX = None


def _api(path, method="GET", body=None, timeout=2.5):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _work_area():
    """主屏工作区（排除任务栏），多屏时按主屏右下角停靠。"""
    try:
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1080


def _single_instance(kind):
    """同一类提醒只留一个窗口：重复拉起时退出，由已存在的窗口自行前置。"""
    global _MUTEX
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                     ctypes.c_wchar_p]
        _MUTEX = k32.CreateMutexW(
            None, False, "ScreenTimeRecorder_PomoPopup_%s" % kind)
        if _MUTEX and k32.GetLastError() == 183:
            return False
    except Exception:
        pass
    return True


def _ellipsize(text, font, max_px):
    text = text or ""
    if font.measure(text) <= max_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + "…") if lo else "…"


class Popup:
    def __init__(self, kind):
        self.kind = kind
        self.state = {}
        self.report = {}
        self._closed = False
        self._auto_close = None

        self.root = tk.Tk()
        self.root.title("番茄钟")
        self.root.overrideredirect(True)
        self.root.configure(bg=LINE)          # 1px 描边
        try:
            self.root.attributes("-topmost", True)
            self.root.attributes("-alpha", 0.0)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.f_big = tkfont.Font(family="Microsoft YaHei UI", size=16,
                                 weight="bold")
        self.f_mid = tkfont.Font(family="Microsoft YaHei UI", size=10)
        self.f_small = tkfont.Font(family="Microsoft YaHei UI", size=8)
        self.f_btn = tkfont.Font(family="Microsoft YaHei UI", size=9)

        self._place()
        self._load()
        self._build()
        self.root.bind("<Escape>", lambda e: self.close())
        self._fade_in()
        if self.kind == "break_done":
            self._auto_close = self.root.after(5000, self.close)
        else:
            # 提醒不会无限期挂在屏幕上；状态仍留在托盘/待办窗口里可继续操作
            self._auto_close = self.root.after(30000, self.close)

    # ---- 位置与数据 ----
    def _place(self):
        left, top, right, bottom = _work_area()
        x = max(left + 8, right - WIDTH - 20)
        y = max(top + 8, bottom - HEIGHT - 20)
        self.root.geometry("%dx%d+%d+%d" % (WIDTH, HEIGHT, x, y))

    def _load(self):
        try:
            self.state = _api("/api/pomodoro") or {}
        except Exception:
            self.state = {}
        sid = self.state.get("session_id")
        if self.kind == "focus_done" and sid:
            try:
                data = _api("/api/pomodoro/report/%d" % int(sid)) or {}
                self.report = data.get("report") or {}
            except Exception:
                self.report = {}

    # ---- 布局 ----
    def _build(self):
        card = tk.Frame(self.root, bg=CARD)
        card.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=14, pady=(10, 0))
        dot = tk.Canvas(head, width=10, height=10, bg=CARD,
                        highlightthickness=0, bd=0)
        dot.pack(side="left", pady=(3, 0))
        dot.create_oval(1, 1, 9, 9, fill=ACCENT, outline="")
        tk.Label(head, text="番茄钟", font=self.f_small, fg=MUTED,
                 bg=CARD).pack(side="left", padx=(6, 0))
        close = tk.Label(head, text="×", font=("Microsoft YaHei UI", 12),
                         fg=MUTED, bg=CARD, cursor="hand2", padx=4)
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.close())

        if self.kind == "break_done":
            self._build_break_done(card)
        else:
            self._build_focus_done(card)

    def _build_focus_done(self, card):
        st = self.state
        minutes = int(st.get("focus_minutes") or 0)
        if not minutes and st.get("planned_ms"):
            minutes = int(st["planned_ms"]) // 60000
        title = (st.get("title") or "").strip()

        head = ("专注 %d 分钟完成" % minutes) if minutes else "专注完成"
        tk.Label(card, text=head,
                 font=self.f_big, fg=INK, bg=CARD, anchor="w").pack(
            fill="x", padx=14, pady=(6, 0))

        # 副标题：待办名 + 专注率
        parts = []
        if title:
            parts.append(_ellipsize(title, self.f_mid, 190))
        rate = self.report.get("rate")
        if rate is not None:
            parts.append("专注率 %d%%" % round(rate * 100))
        else:
            parts.append("本轮已记录")
        tk.Label(card, text=" · ".join(parts), font=self.f_mid, fg=MUTED,
                 bg=CARD, anchor="w").pack(fill="x", padx=14, pady=(2, 0))

        # 一句人话结论（有采集数据时才有）
        tip = (self.report.get("text") or "").strip()
        if tip:
            tk.Label(card, text=_ellipsize(tip, self.f_small, 340),
                     font=self.f_small, fg="#9A8F73",
                     bg=CARD, anchor="w").pack(fill="x", padx=14, pady=(4, 0))

        bar = tk.Frame(card, bg=CARD)
        bar.pack(fill="x", padx=14, pady=(12, 12))
        bm = int(st.get("break_minutes") or 0)

        def mkbtn(text, fn, primary=False):
            b = tk.Label(bar, text=text, font=self.f_btn,
                         fg="#FFFFFF" if primary else MUTED,
                         bg=ACCENT if primary else BG,
                         padx=12, pady=6, cursor="hand2")
            b.pack(side="left", padx=(0, 6))
            b.bind("<Button-1>", lambda e: fn())
            if primary:
                b.bind("<Enter>", lambda e: b.configure(bg=ACCENT_DARK))
                b.bind("<Leave>", lambda e: b.configure(bg=ACCENT))
            return b

        if bm > 0:
            mkbtn("休息 %d 分，然后继续" % bm, self._rest_and_next, True)
        else:
            mkbtn("开始下一轮", self._next_round, True)
        mkbtn("直接下一轮", self._next_round)
        mkbtn("结束", self._finish)

    def _build_break_done(self, card):
        nxt = self.state.get("next_round") or {}
        # 自动开下一轮后 next_round 已被清空，优先读当前会话（即新一轮）
        fm = int(self.state.get("focus_minutes")
                 or nxt.get("focus_minutes")
                 or (self.state.get("config") or {}).get("focus_minutes") or 0)
        title = (self.state.get("title") or nxt.get("title") or "").strip()

        tk.Label(card, text="休息结束", font=self.f_big, fg=INK, bg=CARD,
                 anchor="w").pack(fill="x", padx=14, pady=(10, 0))
        sub = "已开始下一个番茄：专注 %d 分钟" % fm if fm else "准备开始下一个番茄"
        tk.Label(card, text=sub, font=self.f_mid, fg=MUTED, bg=CARD,
                 anchor="w").pack(fill="x", padx=14, pady=(2, 0))
        if title:
            tk.Label(card, text=_ellipsize(title, self.f_small, 340),
                     font=self.f_small, fg="#9A8F73", bg=CARD,
                     anchor="w").pack(fill="x", padx=14, pady=(4, 0))
        tk.Label(card, text="5 秒后自动收起", font=self.f_small,
                 fg="#B5AC97", bg=CARD, anchor="w").pack(
            fill="x", padx=14, pady=(12, 0))

    # ---- 动作 ----
    def _act(self, path, body=None):
        try:
            _api(path, "POST", body or {})
        except Exception:
            pass
        self.close()

    def _rest_and_next(self):
        self._act("/api/pomodoro/start-break", {"chain": True})

    def _next_round(self):
        self._act("/api/pomodoro/next")

    def _finish(self):
        self._act("/api/pomodoro/skip-break")

    # ---- 生命周期 ----
    def _fade_in(self, step=0):
        if self._closed:
            return
        try:
            self.root.attributes("-alpha", min(1.0, 0.25 + step * 0.25))
            self.root.lift()
        except Exception:
            pass
        if step < 3:
            self.root.after(24, lambda: self._fade_in(step + 1))

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._auto_close:
            try:
                self.root.after_cancel(self._auto_close)
            except Exception:
                pass
            self._auto_close = None
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def main():
    kind = "focus_done"
    for i, a in enumerate(sys.argv):
        if a == "--kind" and i + 1 < len(sys.argv):
            kind = sys.argv[i + 1]
    if kind not in ("focus_done", "break_done"):
        kind = "focus_done"
    if not _single_instance(kind):
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        Popup(kind).run()
    except Exception as e:
        # pythonw 下没有控制台，落一份短日志方便排查
        try:
            log = os.path.join(os.path.dirname(config.DB_PATH),
                               "pomodoro_popup.log")
            with open(log, "a", encoding="utf-8") as f:
                f.write("[popup] %s: %s\n" % (kind, e))
        except Exception:
            pass


if __name__ == "__main__":
    main()
