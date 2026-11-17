# -*- coding: utf-8 -*-
"""待办窗口底部的番茄钟面板（Mixin）。

为什么用 Mixin 而不是独立组件类：
面板要读窗口的 _pomo 状态、重建 pomo_area、触发列表行重绘，和 TodoApp 的
耦合点很多。拆成独立类会变成十几个回调的转接；Mixin 让方法仍写在
self 上，调用关系不变，只是把 350 行从主窗口文件里挪出来。

主窗口需要提供：root / bottom / pomo_area / pomo_btn / _f_small /
_pomo* 状态字段 / _last_sig / _editing / refresh()。
"""
import time

import tkinter as tk
from tkinter import messagebox

from . import config
from .pomodoro import fmt_mmss
from .todo_theme import (ACCENT, BG, CARD, DANGER, FAINT, F_BODY, F_SMALL,
                         INK, MUTED)
from .todo_utils import api, ellipsize


class PomodoroPanelMixin:
    # ---- 轮询与每秒刷新 ----
    def _schedule_pomo(self):
        try:
            self._pomo_after = self.root.after(3000, self._poll_pomo)
        except Exception:
            self._pomo_after = None

    def _poll_pomo(self):
        if self._pomo_after:
            try:
                self.root.after_cancel(self._pomo_after)
            except Exception:
                pass
            self._pomo_after = None
        try:
            st = api("/api/pomodoro", timeout=2.0)
            self._pomo = st
            self._pomo_sync_mono = time.monotonic()
            # 专注状态/绑定的待办变了 → 行内按钮文案与倒计时需要重绘。
            # 编辑中不更新 key，等下一轮 poll 再补，避免状态被永久跳过。
            key = (st.get("state"), st.get("todo_id"), st.get("session_id"))
            if key != self._pomo_prev_key and not self._editing:
                self._pomo_prev_key = key
                self._last_sig = None
                self.refresh()
            if st.get("open_setup") and st.get("state") == "idle":
                self._pomo_setup_open = True
                self._pomo_sig = None
                if not self._pomo_setup_acked:
                    self._pomo_setup_acked = True
                    try:
                        api("/api/pomodoro/ack-setup", "POST", {})
                    except Exception:
                        pass
            else:
                self._pomo_setup_acked = False
                if st.get("state") != "idle":
                    self._pomo_setup_open = False
            self._render_pomo()
        except Exception:
            pass          # 主进程没起/瞬时失败：保持上次显示
        self._schedule_pomo()

    def _tick_pomo(self):
        self._pomo_tick_after = None
        st = self._pomo
        if (st and st.get("state") in ("focus", "break")
                and (self._pomo_lbl is not None
                     or self._row_pomo_lbl is not None)):
            remain = int(st.get("remaining_ms", 0) -
                         (time.monotonic() - self._pomo_sync_mono) * 1000)
            text = fmt_mmss(max(0, remain))
            for lbl in (self._pomo_lbl, self._row_pomo_lbl):
                if lbl is None:
                    continue
                try:
                    lbl.configure(text=text)
                except Exception:
                    pass
        try:
            self._pomo_tick_after = self.root.after(1000, self._tick_pomo)
        except Exception:
            pass

    # ---- 面板渲染 ----
    def _render_pomo(self):
        st = self._pomo or {}
        state = st.get("state", "idle")
        setup = self._pomo_setup_open and state == "idle"
        sig = (state, setup, st.get("session_id"), st.get("title"),
               st.get("break_minutes"), st.get("focus_minutes"))
        if sig == self._pomo_sig:
            return
        self._pomo_sig = sig

        for w in self.pomo_area.winfo_children():
            w.destroy()
        self._pomo_lbl = None
        if state == "idle" and not setup:
            try:
                self.pomo_area.pack_forget()
            except Exception:
                pass
            self._update_pomo_btn(state, setup)
            return
        try:
            self.pomo_area.pack(fill="x", padx=12, pady=(0, 4),
                                before=self.bottom)
        except Exception:
            try:
                self.pomo_area.pack(fill="x", padx=12, pady=(0, 4))
            except Exception:
                pass

        if setup:
            self._render_pomo_setup(st)
        elif state == "focus_done":
            self._render_pomo_done(st)
        else:
            self._render_pomo_running(st)
        self._update_pomo_btn(state, setup)

    def _update_pomo_btn(self, state, setup):
        try:
            if state == "idle":
                self.pomo_btn.configure(
                    text="收起" if setup else "番茄钟",
                    fg=ACCENT if setup else MUTED, bg=BG)
            elif state == "paused":
                self.pomo_btn.configure(text="已暂停", fg=DANGER, bg=BG)
            elif state == "focus_done":
                self.pomo_btn.configure(text="待选择", fg=ACCENT, bg=BG)
            else:
                self.pomo_btn.configure(text="进行中", fg=ACCENT, bg=BG)
        except Exception:
            pass

    def _render_pomo_setup(self, st):
        pad = tk.Frame(self.pomo_area, bg=CARD, highlightthickness=1,
                       highlightbackground=FAINT)
        pad.pack(fill="x")
        pad.columnconfigure(1, weight=1)
        cfg = st.get("config") or {}
        self._focus_var = tk.StringVar(
            value=str(cfg.get("focus_minutes",
                              config.POMODORO_DEFAULT_FOCUS_MIN)))
        self._break_var = tk.StringVar(
            value=str(cfg.get("break_minutes",
                              config.POMODORO_DEFAULT_BREAK_MIN)))

        tk.Label(pad, text="专注", font=F_SMALL, fg=MUTED, bg=CARD).grid(
            row=0, column=0, sticky="w", padx=(10, 4), pady=(8, 3))
        self._stepper(pad, self._focus_var, config.POMODORO_FOCUS_MIN,
                      config.POMODORO_FOCUS_MAX,
                      config.POMODORO_DEFAULT_FOCUS_MIN).grid(
            row=0, column=1, sticky="e", padx=(0, 10), pady=(8, 3))

        tk.Label(pad, text="休息", font=F_SMALL, fg=MUTED, bg=CARD).grid(
            row=1, column=0, sticky="w", padx=(10, 4), pady=(2, 3))
        self._stepper(pad, self._break_var, config.POMODORO_BREAK_MIN,
                      config.POMODORO_BREAK_MAX,
                      config.POMODORO_DEFAULT_BREAK_MIN).grid(
            row=1, column=1, sticky="e", padx=(0, 10), pady=(2, 3))

        tk.Label(pad, text="点列表右侧「专注」可直接绑定待办",
                 font=("Microsoft YaHei UI", 8), fg=MUTED, bg=CARD,
                 anchor="w").grid(row=2, column=0, columnspan=2,
                                  sticky="w", padx=10)
        tk.Label(pad, text="步长 5 分钟 · 休息 0 = 只专注不休息",
                 font=("Microsoft YaHei UI", 8), fg=FAINT, bg=CARD,
                 anchor="w").grid(row=3, column=0, columnspan=2,
                                  sticky="w", padx=10)

        bar = tk.Frame(pad, bg=CARD)
        bar.grid(row=4, column=0, columnspan=2, sticky="ew",
                 padx=10, pady=(7, 9))
        start = tk.Label(bar, text="开始专注", font=F_SMALL, fg="#FFFFFF",
                         bg=ACCENT, padx=14, pady=4, cursor="hand2")
        start.pack(side="left")
        start.bind("<Button-1>", lambda e: self._start_pomo_from_panel())
        cancel = tk.Label(bar, text="取消", font=F_SMALL, fg=MUTED,
                          bg=CARD, padx=8, pady=4, cursor="hand2")
        cancel.pack(side="right")
        cancel.bind("<Button-1>", lambda e: self._close_pomo_setup())

    def _stepper(self, parent, var, lo, hi, default):
        box = tk.Frame(parent, bg=CARD)

        def bump(delta):
            try:
                v = int(str(var.get()).strip())
            except (TypeError, ValueError):
                v = default
            var.set(str(max(lo, min(hi, v + delta))))

        minus = tk.Label(box, text="−", font=F_BODY, fg=MUTED, bg=BG,
                         padx=6, pady=2, cursor="hand2")
        minus.pack(side="left")
        minus.bind("<Button-1>", lambda e: bump(-config.POMODORO_STEP_MIN))
        e = tk.Entry(box, textvariable=var, width=4, font=F_BODY, bg=CARD,
                     fg=INK, relief="flat", justify="center",
                     insertbackground=INK, highlightthickness=1,
                     highlightbackground=FAINT, highlightcolor=ACCENT)
        e.pack(side="left", ipady=3, padx=6)
        e.bind("<FocusOut>", lambda ev: self._pomo_read(var, lo, hi, default))
        plus = tk.Label(box, text="+", font=F_BODY, fg=MUTED, bg=BG,
                        padx=6, pady=2, cursor="hand2")
        plus.pack(side="left")
        plus.bind("<Button-1>", lambda e: bump(config.POMODORO_STEP_MIN))
        tk.Label(box, text="分钟", font=F_SMALL, fg=MUTED, bg=CARD,
                 padx=2).pack(side="left")
        return box

    @staticmethod
    def _pomo_read(var, lo, hi, default):
        """非法输入静默回退到合法值（不弹错误框）。"""
        try:
            v = int(str(var.get()).strip())
        except (TypeError, ValueError):
            v = default
        v = max(lo, min(hi, v))
        var.set(str(v))
        return v

    def _render_pomo_running(self, st):
        state = st.get("state")
        row = tk.Frame(self.pomo_area, bg=CARD, highlightthickness=1,
                       highlightbackground=FAINT)
        row.pack(fill="x")
        self._pomo_lbl = tk.Label(
            row, text=fmt_mmss(st.get("remaining_ms", 0)),
            font=("Microsoft YaHei UI", 15, "bold"),
            fg=DANGER if state == "paused" else INK, bg=CARD, padx=10)
        self._pomo_lbl.pack(side="left", pady=6)

        def mkbtn(text, fn, fg=MUTED):
            b = tk.Label(row, text=text, font=F_SMALL, fg=fg, bg=CARD,
                         padx=6, pady=4, cursor="hand2")
            b.pack(side="right", padx=(0, 6))
            b.bind("<Button-1>", lambda e: fn())
            return b

        if state == "break":
            mkbtn("结束休息", lambda: self._pomo_action(
                "/api/pomodoro/skip-break"), DANGER)
            nxt = st.get("next_round") or {}
            fm = int(nxt.get("focus_minutes") or 0)
            label = ("休息中 · 然后 %d 分钟" % fm) if (
                st.get("chain_next") and fm) else "休息中"
        else:
            mkbtn("结束", lambda: self._pomo_action(
                "/api/pomodoro/stop", {"reason": "manual"}), DANGER)
            if state == "paused":
                mkbtn("继续", lambda: self._pomo_action(
                    "/api/pomodoro/resume"), ACCENT)
                label = "已暂停"
            else:
                mkbtn("暂停", lambda: self._pomo_action(
                    "/api/pomodoro/pause"), MUTED)
                label = ""
        title = st.get("title") or "自由专注"
        text = label or ellipsize(title, self._f_small, 118)
        tk.Label(row, text=text, font=F_SMALL, fg=MUTED, bg=CARD,
                 anchor="w").pack(side="left", fill="x", expand=True)

    def _render_pomo_done(self, st):
        box = tk.Frame(self.pomo_area, bg=CARD, highlightthickness=1,
                       highlightbackground=FAINT)
        box.pack(fill="x")
        nxt = st.get("next_round") or {}
        title = (nxt.get("title") or "").strip()
        done = int(nxt.get("focus_minutes") or st.get("focus_minutes") or 0)

        top = tk.Frame(box, bg=CARD)
        top.pack(fill="x", padx=10, pady=(8, 1))
        tk.Label(top, text="完成 %d 分钟" % done, font=F_SMALL,
                 fg=INK, bg=CARD).pack(side="left")
        if title:
            tk.Label(top, text=" · " + ellipsize(title, self._f_small, 130),
                     font=F_SMALL, fg=MUTED, bg=CARD, anchor="w").pack(
                side="left")

        bar = tk.Frame(box, bg=CARD)
        bar.pack(fill="x", padx=10, pady=(3, 8))

        def mkbtn(text, fn, fg=MUTED, bg=BG):
            b = tk.Label(bar, text=text, font=F_SMALL, fg=fg, bg=bg,
                         padx=8, pady=4, cursor="hand2")
            b.pack(side="left", padx=(0, 5))
            b.bind("<Button-1>", lambda e: fn())
            return b

        bm = int(st.get("break_minutes") or 0)
        if bm > 0:
            mkbtn("休息 %d 分 → 继续" % bm,
                  lambda: self._pomo_action("/api/pomodoro/start-break",
                                            {"chain": True}),
                  "#FFFFFF", ACCENT)
        else:
            mkbtn("开始下一轮",
                  lambda: self._pomo_action("/api/pomodoro/next"),
                  "#FFFFFF", ACCENT)
        mkbtn("直接下一轮",
              lambda: self._pomo_action("/api/pomodoro/next"))
        mkbtn("结束", lambda: self._pomo_action("/api/pomodoro/skip-break"))
        self._pomo_lbl = None

    # ---- 面板开关 ----
    def _toggle_pomo_setup(self):
        st = self._pomo or {}
        if st.get("state", "idle") != "idle":
            return
        self._pomo_setup_open = not self._pomo_setup_open
        self._pomo_sig = None
        self._render_pomo()

    def _open_pomo_setup(self):
        self._pomo_setup_open = True
        self._pomo_sig = None
        self._render_pomo()

    def _close_pomo_setup(self):
        self._pomo_setup_open = False
        self._pomo_sig = None
        self._render_pomo()

    def _start_pomo_from_panel(self):
        fm = self._pomo_read(self._focus_var, config.POMODORO_FOCUS_MIN,
                             config.POMODORO_FOCUS_MAX,
                             config.POMODORO_DEFAULT_FOCUS_MIN)
        bm = self._pomo_read(self._break_var, config.POMODORO_BREAK_MIN,
                             config.POMODORO_BREAK_MAX,
                             config.POMODORO_DEFAULT_BREAK_MIN)
        self._pomo_setup_open = False
        self._pomo_action("/api/pomodoro/start",
                          {"focus_minutes": fm, "break_minutes": bm})

    def _pomo_remain_text(self):
        st = self._pomo or {}
        remain = int(st.get("remaining_ms", 0) -
                     (time.monotonic() - self._pomo_sync_mono) * 1000)
        return fmt_mmss(max(0, remain))

    def _on_focus_click(self, item):
        """行内「专注」：绑定该待办直接开始；已绑定的那行则展开控制面板。"""
        st = self._pomo or {}
        state = st.get("state", "idle")
        if state in ("focus", "paused") and st.get("todo_id") == item["id"]:
            self._open_pomo_panel()
            return
        if state in ("focus", "paused", "break") and st.get("session_id"):
            cur = (st.get("title")
                   or ("休息中" if state == "break" else "自由专注"))
            if not messagebox.askyesno(
                    "番茄钟",
                    "正在「%s」，要中断并开始「%s」吗？"
                    % (cur, item["title"]), parent=self.root):
                return
        cfg = st.get("config") or {}
        self._pomo_action("/api/pomodoro/start", {
            "todo_id": item["id"],
            "focus_minutes": cfg.get("focus_minutes"),
            "break_minutes": cfg.get("break_minutes"),
        })

    def _open_pomo_panel(self):
        self._pomo_setup_open = False
        self._pomo_sig = None
        self._render_pomo()

    def _pomo_action(self, path, body=None):
        try:
            api(path, "POST", body or {})
        except Exception as e:
            messagebox.showerror("番茄钟", str(e), parent=self.root)
            return
        self._pomo_sig = None
        self._poll_pomo()
