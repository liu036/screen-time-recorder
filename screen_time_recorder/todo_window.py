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
import threading
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
# 优先级：低 / 中 / 高（高 = 红色）
PRIO_COLORS = ("#D9D3C4", "#43593B", "#C0553B")
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


def next_priority(current):
    """低(0) → 中(1) → 高(2) → 低(0)。

    注意不能写 `current or 1`：0 会被吞成 1，循环就退化成灰/高两态。
    """
    cur = min(max(int(current if current is not None else 1), 0), 2)
    return (cur + 1) % 3


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
        self._last_sig = None
        self._last_w = None
        self._resize_after = None
        self._editing = False       # 行内编辑/新建期间暂停轮询重建
        self._write_seq = 0         # 丢弃过期写请求的回读结果
        # 字体对象缓存：每次渲染新建 tkfont.Font 会不断累积在 Tk 解释器里
        self._f_title = tkfont.Font(family="Microsoft YaHei UI", size=10)
        self._f_title_hi = tkfont.Font(family="Microsoft YaHei UI", size=10,
                                        weight="bold")
        self._f_small = tkfont.Font(family="Microsoft YaHei UI", size=8)
        self._restore_geometry()
        self._build()
        self.refresh()
        if focus_input:
            self.root.after(120, self._start_new_row)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<F5>", lambda e: self.refresh())
        self.root.bind("<Configure>", self._on_resize)

    # ---- 布局 ----
    def _build(self):
        # 筛选（保留现状）
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

        # 底部：状态 + 纯 + 号新建按钮
        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="x", padx=12, pady=(4, 10))
        self.status = tk.Label(bottom, text="", font=F_SMALL, fg=MUTED,
                               bg=BG, anchor="w")
        self.status.pack(side="left")
        self.add_btn = tk.Canvas(bottom, width=34, height=34, bg=BG,
                                 highlightthickness=0, bd=0, cursor="hand2")
        self.add_btn.pack(side="right")
        self._draw_add_btn(False)
        self.add_btn.bind("<Button-1>", lambda e: self._start_new_row())
        self.add_btn.bind("<Enter>", lambda e: self._draw_add_btn(True))
        self.add_btn.bind("<Leave>", lambda e: self._draw_add_btn(False))

    def _draw_add_btn(self, hover):
        """纯 + 号圆形按钮（取代常驻输入框）。"""
        c = self.add_btn
        c.delete("all")
        color = "#B5650F" if hover else ACCENT
        c.create_oval(2, 2, 32, 32, fill=color, outline=color)
        c.create_line(11, 17, 23, 17, fill="#FFFFFF", width=2)
        c.create_line(17, 11, 17, 23, fill="#FFFFFF", width=2)

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)

    def _on_resize(self, e):
        """窗口宽度变化 → 标题可用宽度变了，强制重绘一次（防抖 180ms）。"""
        if e.widget is not self.root or e.width <= 1:
            return
        if e.width == self._last_w:
            return
        self._last_w = e.width
        self._last_sig = None
        if self._resize_after:
            try:
                self.root.after_cancel(self._resize_after)
            except Exception:
                pass
        self._resize_after = self.root.after(180, self.refresh)

    def _on_wheel(self, e):
        try:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass

    # ---- 刷新与异步写入 ----
    def _schedule(self):
        self._after_id = self.root.after(config.TODO_POLL_MS, self.refresh)

    def _write(self, path, method, body, on_success=None):
        """后台线程完成写操作并回读视图数据；主线程只更新签名/状态，不重建 UI。

        Tk 是单线程事件循环，任何同步 HTTP 都会冻结界面，所以写操作一律异步。
        """
        flt = self.filter
        self._write_seq += 1
        seq = self._write_seq

        def worker():
            try:
                result = api(path, method, body)
                data = api("/api/todos?filter=" + flt)["todos"]
                summary = api("/api/todos/summary")
                err = None
            except Exception as ex:
                result = data = summary = None
                err = ex
            try:
                self.root.after(0, lambda: self._after_write(
                    result, data, summary, err, on_success, seq))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True,
                         name="todo-write").start()

    def _after_write(self, result, data, summary, err,
                     on_success=None, seq=None):
        if err is not None:
            messagebox.showerror("操作失败", str(err), parent=self.root)
            self._last_sig = None
            self.refresh()          # 失败才回滚显示
            return
        if on_success is not None:
            try:
                on_success(result)
            except Exception:
                pass
        # 快速连点时只认最后一次写的回读结果，避免状态被旧请求回退
        if seq is not None and seq != self._write_seq:
            return
        if data is not None:
            self._rows = data
            self._last_sig = json.dumps(
                [data, summary], sort_keys=True, ensure_ascii=False)
            self._update_status(summary)

    def refresh(self):
        if self._after_id:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        # 行内编辑/新建期间完全跳过（连请求都不发），避免输入框被重建
        if self._editing:
            self._schedule()
            return
        try:
            data = api("/api/todos?filter=" + self.filter)["todos"]
            summary = api("/api/todos/summary")
        except (urllib.error.URLError, OSError, ValueError) as e:
            # 离线时只渲染一次，避免每 3 秒重建错误页
            if not self._offline:
                self._offline = True
                self._last_sig = None
                self._render_offline(e)
            self._schedule()
            return
        self._offline = False
        # 数据没变就不重建 DOM：省内存/CPU，也不会打断滚动和悬停
        sig = json.dumps([data, summary], sort_keys=True, ensure_ascii=False)
        if sig != self._last_sig:
            self._last_sig = sig
            self._render(data, summary)
        self._schedule()

    def _render(self, todos, summary):
        pos = self.canvas.yview()[0] if self._rows else 0.0
        for w in self.inner.winfo_children():
            w.destroy()
        self._rows = todos
        if not todos:
            tk.Label(self.inner, text="没有待办，点右下角 ＋ 添加",
                     font=F_BODY, fg=MUTED, bg=BG, pady=24).pack()
        else:
            for item in todos:
                self._render_row(item)
        self._update_tabs(summary)
        self._update_status(summary)
        # 不在这里强制 update_idletasks()：它会同步重排全部子控件，
        # 20 行约 100ms，是点击后界面卡顿的主因。滚动区域交给空闲回调。
        self.root.after_idle(self._on_inner)
        if pos:
            self.canvas.yview_moveto(pos)

    def _update_status(self, summary):
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

    def _render_row(self, item):
        # outer 把「行 + 分隔线」包成整体，删除时可一次销毁
        outer = tk.Frame(self.inner, bg=BG)
        outer.pack(fill="x")
        row = tk.Frame(outer, bg=BG, height=46)
        row.pack(fill="x", padx=8)
        row.pack_propagate(False)

        is_done = item["status"] == "done"
        # 完成勾选（绘制延后到 title 就绪后）
        chk = tk.Canvas(row, width=20, height=20, bg=BG,
                        highlightthickness=0, bd=0, cursor="hand2")
        chk.pack(side="left", padx=(4, 8))

        # 优先级圆点：点击循环 低 → 中 → 高(红) → 低
        pr = item.get("priority", 1)
        pr = min(max(int(pr if pr is not None else 1), 0), 2)
        pri = tk.Canvas(row, width=14, height=20, bg=BG,
                        highlightthickness=0, bd=0, cursor="hand2")
        pri.pack(side="left", padx=(0, 6))
        oval = pri.create_oval(3, 7, 11, 15, fill=PRIO_COLORS[pr], outline="")

        # 先放右侧（时间/删除）：pack 按调用顺序分配空间，
        # 若标题先 pack 并 expand，长标题会把右侧控件挤出可视区。
        ts = item.get("remind_ts") or item.get("due_ts")
        ts_text = fmt_when(ts) if ts else ""
        dele = tk.Label(row, text="×", font=("Microsoft YaHei UI", 12),
                        fg=CHK_BORDER, bg=BG, cursor="hand2", padx=4)
        dele.pack(side="right", padx=(0, 2))
        time_lbl = None
        if ts_text:
            overdue = (not is_done) and ts < datetime.now().timestamp() * 1000
            time_lbl = tk.Label(row, text=ts_text, font=F_SMALL, bg=BG,
                                fg=DANGER if overdue else MUTED)
            time_lbl.pack(side="right", padx=(6, 2))

        # 标题：单击就地编辑（不再开模态对话框）
        font = self._f_title_hi if (pr == 2 and not is_done) else self._f_title
        win_w = self.root.winfo_width()
        if win_w <= 1:  # 首次渲染时窗口尚未映射
            win_w = config.TODO_WINDOW_SIZE[0]
        avail = max(80, win_w - 90)
        if time_lbl is not None:
            avail -= self._f_small.measure(ts_text) + 14
        shown = _ellipsize(item["title"], font, avail)
        title = tk.Label(row, text=shown, font=font,
                         fg=DONE if is_done else INK,
                         bg=BG, anchor="w", justify="left", cursor="xterm")
        title.pack(side="left", fill="x", expand=True)

        # 局部重绘完成状态，不重建整行
        def set_done(done):
            chk.delete("all")
            if done:
                chk.create_oval(2, 2, 18, 18, fill=DONE, outline=DONE)
                chk.create_line(6, 10, 9, 13, 14, 6, fill="#FFFFFF", width=2)
                title.configure(fg=DONE, font=self._f_title)
            else:
                chk.create_oval(2, 2, 18, 18, outline=CHK_BORDER, width=1.6)
                title.configure(fg=INK, font=font)

        set_done(is_done)
        chk.bind("<Button-1>",
                 lambda e, t=item, f=set_done: self.toggle_done(t, f))
        pri.bind("<Button-1>",
                 lambda e, t=item, c=pri, o=oval:
                 self._cycle_priority(t, c, o))
        dele.bind("<Button-1>",
                  lambda e, t=item, w=outer: self.on_delete(t, w))
        title.bind("<Button-1>",
                   lambda e, t=item, w=title, f=font:
                   self._start_inline_edit(t, w, f))

        # 悬停高亮（编辑期间跳过，避免反复重绘干扰输入）
        def enter(_e, r=row):
            if self._editing:
                return
            r.configure(bg=CARD)
            dele.configure(fg=DANGER)
            for c in r.winfo_children():
                if isinstance(c, (tk.Label, tk.Frame)) and c is not dele:
                    try:
                        c.configure(bg=CARD)
                    except Exception:
                        pass

        def leave(_e, r=row):
            if self._editing:
                return
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

        tk.Frame(outer, bg=LINE, height=1).pack(fill="x", padx=14)
        return outer

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

    def _start_new_row(self):
        """点击右下角 ＋：在列表末尾插入一行输入框，不切视图。"""
        if self._editing:
            return
        self._editing = True
        row = tk.Frame(self.inner, bg=BG, height=46)
        row.pack(fill="x", padx=8)
        row.pack_propagate(False)
        e = tk.Entry(row, font=F_TITLE, bg=CARD, fg=INK, relief="flat",
                     insertbackground=INK, highlightthickness=1,
                     highlightbackground=ACCENT, highlightcolor=ACCENT)
        e.pack(fill="x", padx=(4, 30), ipady=5, pady=6)

        def reveal():
            self._on_inner()
            self.canvas.yview_moveto(1.0)

        self.root.after_idle(reveal)
        e.focus_set()
        state = {"done": False}

        def finish(save):
            if state["done"]:
                return
            state["done"] = True
            text = e.get().strip()
            row.destroy()
            self._editing = False
            if not (save and text):
                return                       # 取消：列表没变，无需刷新

            def on_ok(result):
                todo = (result or {}).get("todo")
                if todo:
                    self._render_row(todo)    # 只追加一行，不重建列表
                    self.root.after_idle(self._on_inner)

            self._write("/api/todos", "POST", {"title": text},
                        on_success=on_ok)

        e.bind("<Return>", lambda ev: finish(True))
        e.bind("<Escape>", lambda ev: finish(False))
        e.bind("<FocusOut>", lambda ev: finish(True))

    def _start_inline_edit(self, item, label, font=None):
        """单击标题：该行原地变输入框，Enter 保存 / Esc 取消 / 失焦保存。"""
        if self._editing:
            return
        self._editing = True
        font = font or self._f_title
        w = label.winfo_width()
        if w <= 1:
            w = max(120, self.root.winfo_width() - 120)
        e = tk.Entry(label, font=font, bg=CARD, fg=INK, relief="flat",
                     insertbackground=INK, highlightthickness=1,
                     highlightbackground=ACCENT, highlightcolor=ACCENT)
        e.insert(0, item["title"])
        e.select_range(0, "end")
        e.place(x=0, y=-3, width=max(120, w), height=30)
        e.focus_set()
        state = {"done": False, "pending": None}

        def cancel_pending():
            if state["pending"] is not None:
                try:
                    self.root.after_cancel(state["pending"])
                except Exception:
                    pass
                state["pending"] = None

        def commit(save=True):
            if state["done"]:
                return
            state["done"] = True
            cancel_pending()
            text = e.get().strip()
            e.destroy()
            self._editing = False
            if not (save and text and text != item["title"]):
                return                        # 无改动：不写库、不重建
            # 乐观更新：立刻改标题文字，网络在后台完成
            try:
                label.configure(
                    text=_ellipsize(text, font,
                                    max(80, label.winfo_width())))
            except Exception:
                pass
            item["title"] = text
            self._write("/api/todos/%d" % item["id"], "PATCH",
                        {"title": text})

        def on_focus_out(_ev):
            # 延迟 80ms：避免点击瞬间焦点抖动把输入框立刻提交掉
            if state["done"]:
                return
            cancel_pending()
            state["pending"] = self.root.after(80, lambda: commit(True))

        e.bind("<Return>", lambda ev: commit(True))
        e.bind("<Escape>", lambda ev: commit(False))
        e.bind("<FocusOut>", on_focus_out)
        e.bind("<FocusIn>", lambda ev: cancel_pending())

    def _cycle_priority(self, item, canvas, oval):
        """点击优先级圆点：低 → 中 → 高(红) → 低，局部改色 + 后台写库。"""
        cur = next((t.get("priority", 1) for t in self._rows
                    if t["id"] == item["id"]), 1)
        new = next_priority(cur)
        try:
            canvas.itemconfigure(oval, fill=PRIO_COLORS[new])
        except Exception:
            pass
        for t in self._rows:
            if t["id"] == item["id"]:
                t["priority"] = new
                break
        self._write("/api/todos/%d" % item["id"], "PATCH",
                    {"priority": new})

    def toggle_done(self, item, paint=None):
        done = item["status"] != "done"
        if paint is not None:
            paint(done)                       # 局部重绘该行圆圈/标题
        item["status"] = "done" if done else "open"
        self._write("/api/todos/%d/done" % item["id"], "POST",
                    {"done": done})

    def on_delete(self, item, widget=None):
        """点 × 直接删除：局部销毁该行，无确认弹窗、无撤销。"""
        if widget is not None:
            try:
                widget.destroy()
            except Exception:
                pass
        self._rows = [t for t in self._rows if t["id"] != item["id"]]
        self._write("/api/todos/%d" % item["id"], "DELETE", None)

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
