# -*- coding: utf-8 -*-
"""待办便笺窗（独立进程）。

为什么独立进程而不是放进主进程：
- 主进程主线程被 win32gui.PumpMessages() 占用，Tk 也要求主线程跑 mainloop；
- 采集器与窗口崩溃隔离：窗口挂了不影响记录。

数据全部走主进程的 Flask API（127.0.0.1:8765），保证「主进程是唯一写入方」，
避免 SQLite 多进程写冲突。

启动：
    pythonw -m screen_time_recorder.todo_window [--focus]
"""
import ctypes
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import tkinter as tk
from tkinter import messagebox
from tkinter import font as tkfont

from . import config

API_BASE = os.environ.get("STR_API", "http://127.0.0.1:8765")
WINDOW_TITLE = "待办"
MUTEX_NAME = "ScreenTimeRecorder_TodoWindow"
STATE_PATH = os.path.join(os.path.dirname(config.DB_PATH), "todo_window.json")

# palm 配色（与仪表盘同一套语义）
BG = "#F5F2EA"
CARD = "#FFFDF8"
INK = "#2B2B28"
MUTED = "#8A8578"
FAINT = "#E2DCCC"
LINE = "#EAE4D5"
CHK_BORDER = "#C9C2B0"
ACCENT = "#D4A017"
DANGER = "#C0553B"
DONE = "#A9A395"

F_TITLE = ("Microsoft YaHei UI", 10)
F_TITLE_HI = ("Microsoft YaHei UI", 10, "bold")
F_SMALL = ("Microsoft YaHei UI", 8)
F_BODY = ("Microsoft YaHei UI", 9)

_mutex_handle = None


# ---------- HTTP ----------
def api(path, method="GET", body=None, timeout=3.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


# ---------- 时间处理 ----------
def fmt_when(ts):
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts / 1000.0)
    today = datetime.now().date()
    d = dt.date()
    hm = dt.strftime("%H:%M")
    if d < today:
        days = (today - d).days
        return "逾期 %d 天" % days if days > 1 else "逾期"
    if d == today:
        return "今天 " + hm
    if d == today + timedelta(days=1):
        return "明天 " + hm
    return d.strftime("%m-%d") + " " + hm


def parse_when(text):
    """支持：'2026-11-05 18:00' / '11-05 18:00' / '18:00'；空串 → None。"""
    s = (text or "").strip()
    if not s:
        return None
    now = datetime.now()
    try:
        if len(s) <= 5 and ":" in s:
            h, m = s.split(":")
            cand = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if cand < now:
                cand += timedelta(days=1)
            return int(cand.timestamp() * 1000)
        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m-%d %H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%m-%d %H:%M":
                    dt = dt.replace(year=now.year)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
        dt = datetime.strptime(s, "%Y-%m-%d")
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def to_input(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts / 1000.0).strftime("%Y-%m-%d %H:%M")


def _ellipsize(text, font, max_px):
    """按像素宽度截断并补省略号（Label 不会自动省略，长标题会撑破布局）。"""
    text = text or ""
    if max_px <= 0 or font.measure(text) <= max_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + "…") if lo else "…"


# ---------- 单实例 ----------
def already_running():
    global _mutex_handle
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                     ctypes.c_wchar_p]
        _mutex_handle = k32.CreateMutexW(None, False, MUTEX_NAME)
        if not _mutex_handle:
            return False
        return k32.GetLastError() == 183
    except Exception:
        return False


def focus_existing():
    try:
        import win32con
        import win32gui
        hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
        if hwnd:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


# ---------- 主窗口 ----------
class TodoApp:
    def __init__(self, focus_input=False):
        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.configure(bg=BG)
        self.root.minsize(300, 360)
        self.filter = "today"
        self._after_id = None
        self._offline = False
        self._rows = []
        self._focus_input = focus_input
        self._restore_geometry()
        self._build()
        self.refresh()
        if focus_input:
            self.root.after(120, self.entry.focus_set)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Control-n>", lambda e: self.entry.focus_set())
        self.root.bind("<F5>", lambda e: self.refresh())

    # ---- 布局 ----
    def _build(self):
        # 快速添加
        top = tk.Frame(self.root, bg=BG, padx=12, pady=10)
        top.pack(fill="x")
        self.entry = tk.Entry(
            top, font=F_TITLE, bg=CARD, fg=INK, relief="flat",
            insertbackground=INK, highlightthickness=1,
            highlightbackground=FAINT, highlightcolor=ACCENT)
        self.entry.pack(fill="x", ipady=7)
        self._placeholder = "添加待办，回车确认…"
        self.entry.insert(0, self._placeholder)
        self.entry.configure(fg=MUTED)
        self.entry.bind("<FocusIn>", self._entry_focus_in)
        self.entry.bind("<FocusOut>", self._entry_focus_out)
        self.entry.bind("<Return>", self.on_add)
        tk.Label(top, text="回车添加 · Ctrl+N 聚焦 · 双击编辑",
                 font=F_SMALL, fg=MUTED, bg=BG, anchor="w").pack(
            fill="x", pady=(5, 0))

        # 筛选
        tabs = tk.Frame(self.root, bg=BG, padx=12)
        tabs.pack(fill="x", pady=(0, 6))
        self.tab_btns = {}
        for key, label in (("today", "今日"), ("open", "未完成"), ("all", "全部")):
            b = tk.Label(tabs, text=label, font=F_BODY, bg=BG, fg=MUTED,
                         padx=9, pady=4, cursor="hand2")
            b.pack(side="left", padx=(0, 4))
            b.bind("<Button-1>", lambda e, k=key: self.set_filter(k))
            self.tab_btns[key] = b

        # 列表（Canvas + 内嵌 Frame，支持滚动）
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        self.scroll = tk.Scrollbar(wrap, orient="vertical",
                                   command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        # 底部状态
        self.status = tk.Label(self.root, text="", font=F_SMALL, fg=MUTED,
                               bg=BG, anchor="w", padx=14, pady=8)
        self.status.pack(fill="x")

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)

    def _on_wheel(self, e):
        try:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass

    # ---- 刷新 ----
    def refresh(self):
        if self._after_id:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            data = api("/api/todos?filter=" + self.filter)["todos"]
            summary = api("/api/todos/summary")
            self._offline = False
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._render_offline(e)
            self._after_id = self.root.after(config.TODO_POLL_MS, self.refresh)
            return
        self._render(data, summary)
        self._after_id = self.root.after(config.TODO_POLL_MS, self.refresh)

    def _render(self, todos, summary):
        pos = self.canvas.yview()[0] if self._rows else 0.0
        for w in self.inner.winfo_children():
            w.destroy()
        self._rows = todos
        if not todos:
            tk.Label(self.inner, text="没有待办，上方输入即可添加",
                     font=F_BODY, fg=MUTED, bg=BG, pady=24).pack()
        else:
            for item in todos:
                self._render_row(item)
        self._update_tabs(summary)
        total = summary.get("open", 0)
        overdue = summary.get("overdue", 0)
        done_today = summary.get("done_today", 0)
        parts = ["未完成 %d" % total]
        if overdue:
            parts.append("逾期 %d" % overdue)
        parts.append("今天完成 %d" % done_today)
        self.status.configure(
            text="  ·  ".join(parts),
            fg=DANGER if overdue else MUTED)
        self.inner.update_idletasks()
        self._on_inner()
        if pos:
            self.canvas.yview_moveto(pos)

    def _render_row(self, item):
        row = tk.Frame(self.inner, bg=BG, height=46)
        row.pack(fill="x", padx=8)
        row.pack_propagate(False)

        is_done = item["status"] == "done"
        # 完成勾选（自绘圆形，避免 ttk 主题带来的灰底）
        chk = tk.Canvas(row, width=20, height=20, bg=BG,
                        highlightthickness=0, bd=0, cursor="hand2")
        chk.pack(side="left", padx=(4, 8))
        if is_done:
            chk.create_oval(2, 2, 18, 18, fill=DONE, outline=DONE)
            chk.create_line(6, 10, 9, 13, 14, 6, fill="#FFFFFF", width=2)
        else:
            chk.create_oval(2, 2, 18, 18, outline=CHK_BORDER, width=1.6)
        chk.bind("<Button-1>", lambda e, t=item: self.toggle_done(t))

        # 优先级细条
        pr = item.get("priority", 1)
        bar_color = ACCENT if pr == 2 else (FAINT if pr == 1 else LINE)
        tk.Frame(row, bg=bar_color, width=3).pack(side="left", fill="y",
                                                  pady=12, padx=(0, 8))

        # 先放右侧（时间/删除）：pack 按调用顺序分配空间，
        # 若标题先 pack 并 expand，长标题会把右侧控件挤出可视区。
        ts = item.get("remind_ts") or item.get("due_ts")
        ts_text = fmt_when(ts) if ts else ""
        dele = tk.Label(row, text="×", font=("Microsoft YaHei UI", 12),
                        fg=CHK_BORDER, bg=BG, cursor="hand2", padx=4)
        dele.pack(side="right", padx=(0, 2))
        dele.bind("<Button-1>", lambda e, t=item: self.on_delete(t))
        time_lbl = None
        if ts_text:
            overdue = (not is_done) and ts < datetime.now().timestamp() * 1000
            time_lbl = tk.Label(row, text=ts_text, font=F_SMALL, bg=BG,
                                fg=DANGER if overdue else MUTED)
            time_lbl.pack(side="right", padx=(6, 2))

        # 标题：按可用宽度截断，避免撑破布局
        title_fg = DONE if is_done else INK
        font = F_TITLE_HI if (pr == 2 and not is_done) else F_TITLE
        # 左侧勾选(20+12) + 优先级条(3+8)，右侧删除(≈26)，再留边距
        avail = max(80, self.root.winfo_width() - 85)
        if time_lbl is not None:
            avail -= tkfont.Font(font=F_SMALL).measure(ts_text) + 14
        shown = _ellipsize(item["title"], tkfont.Font(font=font), avail)
        title = tk.Label(row, text=shown, font=font, fg=title_fg,
                         bg=BG, anchor="w", justify="left")
        title.pack(side="left", fill="x", expand=True)

        # 悬停高亮 + 双击编辑
        def enter(_e, r=row):
            r.configure(bg=CARD)
            dele.configure(fg=DANGER)
            for c in r.winfo_children():
                if isinstance(c, (tk.Label, tk.Frame)) and c is not dele:
                    try:
                        c.configure(bg=CARD)
                    except Exception:
                        pass

        def leave(_e, r=row):
            r.configure(bg=BG)
            dele.configure(fg=CHK_BORDER)
            for c in r.winfo_children():
                if isinstance(c, (tk.Label, tk.Frame)) and c is not dele:
                    try:
                        c.configure(bg=BG)
                    except Exception:
                        pass

        for w in (row, title):
            w.bind("<Enter>", enter)
            w.bind("<Leave>", leave)
            w.bind("<Double-Button-1>", lambda e, t=item: self.open_editor(t))

        tk.Frame(self.inner, bg=LINE, height=1).pack(fill="x", padx=14)

    def _render_offline(self, err):
        for w in self.inner.winfo_children():
            w.destroy()
        self._rows = []
        tk.Label(self.inner, text="连接不上记录器",
                 font=F_TITLE_HI, fg=DANGER, bg=BG, pady=(28, 4)).pack()
        tk.Label(self.inner, text="请先启动屏幕时间记录器（托盘图标）。\n%s" % err,
                 font=F_SMALL, fg=MUTED, bg=BG, justify="center").pack()
        self.status.configure(text="离线，每 3 秒重试", fg=DANGER)

    def _update_tabs(self, summary=None):
        for key, b in self.tab_btns.items():
            if key == self.filter:
                b.configure(bg=INK, fg="#F7F4EC")
            else:
                b.configure(bg=BG, fg=MUTED)

    # ---- 动作 ----
    def set_filter(self, key):
        if key == self.filter:
            return
        self.filter = key
        self.refresh()

    def _entry_focus_in(self, _e=None):
        if self.entry.get() == self._placeholder:
            self.entry.delete(0, "end")
            self.entry.configure(fg=INK)

    def _entry_focus_out(self, _e=None):
        if not self.entry.get().strip():
            self.entry.delete(0, "end")
            self.entry.insert(0, self._placeholder)
            self.entry.configure(fg=MUTED)

    def on_add(self, _e=None):
        text = self.entry.get().strip()
        if not text or text == self._placeholder:
            return
        try:
            api("/api/todos", "POST", {"title": text})
        except Exception as e:
            messagebox.showerror("添加失败", str(e), parent=self.root)
            return
        self.entry.delete(0, "end")
        self.entry.configure(fg=INK)
        self.entry.focus_set()
        self.refresh()

    def toggle_done(self, item):
        try:
            api("/api/todos/%d/done" % item["id"], "POST",
                {"done": item["status"] != "done"})
        except Exception as e:
            messagebox.showerror("操作失败", str(e), parent=self.root)
            return
        self.refresh()

    def on_delete(self, item):
        if not messagebox.askyesno("删除待办", "删除「%s」？" % item["title"],
                                   parent=self.root):
            return
        try:
            api("/api/todos/%d" % item["id"], "DELETE")
        except Exception as e:
            messagebox.showerror("删除失败", str(e), parent=self.root)
            return
        self.refresh()

    # ---- 编辑对话框 ----
    def open_editor(self, item):
        dlg = tk.Toplevel(self.root)
        dlg.title("编辑待办")
        dlg.configure(bg=BG)
        dlg.geometry("340x360")
        dlg.transient(self.root)
        dlg.grab_set()

        def field(label, value, show=None):
            tk.Label(dlg, text=label, font=F_SMALL, fg=MUTED, bg=BG,
                     anchor="w").pack(fill="x", padx=16, pady=(10, 2))
            e = tk.Entry(dlg, font=F_BODY, bg=CARD, fg=INK, relief="flat",
                         highlightthickness=1, highlightbackground=FAINT,
                         highlightcolor=ACCENT, show=show)
            e.insert(0, value or "")
            e.pack(fill="x", padx=16, ipady=5)
            return e

        e_title = field("标题", item["title"])
        tk.Label(dlg, text="备注", font=F_SMALL, fg=MUTED, bg=BG,
                 anchor="w").pack(fill="x", padx=16, pady=(10, 2))
        e_note = tk.Text(dlg, height=3, font=F_BODY, bg=CARD, fg=INK,
                         relief="flat", highlightthickness=1,
                         highlightbackground=FAINT, highlightcolor=ACCENT)
        e_note.insert("1.0", item.get("note") or "")
        e_note.pack(fill="x", padx=16)

        e_due = field("截止时间（2026-11-05 18:00，留空=无）",
                      to_input(item.get("due_ts")))
        e_rem = field("提醒时间（18:00 表示今天/明天该时刻）",
                      to_input(item.get("remind_ts")))

        tk.Label(dlg, text="优先级", font=F_SMALL, fg=MUTED, bg=BG,
                 anchor="w").pack(fill="x", padx=16, pady=(10, 2))
        pr = tk.IntVar(value=item.get("priority", 1))
        prow = tk.Frame(dlg, bg=BG)
        prow.pack(fill="x", padx=12)
        for v, label in ((0, "低"), (1, "中"), (2, "高")):
            tk.Radiobutton(prow, text=label, value=v, variable=pr,
                           font=F_SMALL, bg=BG, fg=INK, selectcolor=CARD,
                           activebackground=BG, highlightthickness=0
                           ).pack(side="left")

        def save():
            title = e_title.get().strip()
            if not title:
                messagebox.showwarning("提示", "标题不能为空", parent=dlg)
                return
            body = {
                "title": title,
                "note": e_note.get("1.0", "end").strip(),
                "due_ts": parse_when(e_due.get()),
                "remind_ts": parse_when(e_rem.get()),
                "priority": pr.get(),
            }
            try:
                api("/api/todos/%d" % item["id"], "PATCH", body)
            except Exception as e:
                messagebox.showerror("保存失败", str(e), parent=dlg)
                return
            dlg.destroy()
            self.refresh()

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=16, pady=16)
        tk.Button(btns, text="保存", command=save, font=F_BODY,
                  bg=INK, fg="#F7F4EC", relief="flat", padx=16, pady=5,
                  activebackground=INK, cursor="hand2").pack(side="right")
        tk.Button(btns, text="取消", command=dlg.destroy, font=F_BODY,
                  bg=BG, fg=MUTED, relief="flat", padx=12, pady=5,
                  cursor="hand2").pack(side="right", padx=6)
        e_title.focus_set()
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    # ---- 窗口状态 ----
    def _restore_geometry(self):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
            w, h = st.get("w", config.TODO_WINDOW_SIZE[0]), st.get("h", config.TODO_WINDOW_SIZE[1])
            x, y = st.get("x"), st.get("y")
            if x is None or y is None:
                x, y = self._default_pos(w, h)
            x, y = self._clamp(x, y, w, h)
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            w, h = config.TODO_WINDOW_SIZE
            x, y = self._default_pos(w, h)
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _default_pos(self, w, h):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        return max(0, sw - w - 40), max(60, sh // 5)

    @staticmethod
    def _clamp(x, y, w, h):
        try:
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            x = max(0, min(int(x), max(0, sw - 80)))
            y = max(0, min(int(y), max(0, sh - 80)))
        except Exception:
            pass
        return x, y

    def _save_geometry(self):
        try:
            geo = self.root.geometry()  # WxH+X+Y
            size, x, y = geo.split("+")[0], geo.split("+")[1], geo.split("+")[2]
            w, h = size.split("x")
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"w": int(w), "h": int(h),
                           "x": int(x), "y": int(y)}, f)
        except Exception:
            pass

    def on_close(self):
        self._save_geometry()
        if self._after_id:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    focus = "--focus" in sys.argv
    if already_running():
        if focus_existing():
            return
        # 互斥体存在但找不到窗口（可能正在退出），稍等再试一次
        import time
        time.sleep(0.6)
        if focus_existing():
            return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    TodoApp(focus_input=focus).run()


if __name__ == "__main__":
    main()
