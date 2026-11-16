# -*- coding: utf-8 -*-
"""番茄钟窗口 UI 冒烟：展开时长面板 → 开始 → 暂停/继续 → 结束 → 托盘信号展开。

临时库 + 独立端口，不碰真实数据。
"""
import os
import sys
import tempfile
import threading
import time

import tkinter as tk

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from screen_time_recorder.storage import Storage
from screen_time_recorder.pomodoro import PomodoroService
from screen_time_recorder import web

PORT = 8770
os.environ["STR_API"] = "http://127.0.0.1:%d" % PORT


def walk(widget, cls):
    out = []
    for c in widget.winfo_children():
        if isinstance(c, cls):
            out.append(c)
        out.extend(walk(c, cls))
    return out


def find_label(parent, text):
    for c in parent.winfo_children():
        if isinstance(c, tk.Label) and c.cget("text") == text:
            return c
        hit = find_label(c, text)
        if hit is not None:
            return hit
    return None


def main():
    st = Storage(os.path.join(tempfile.mkdtemp(), "pui.db"))
    st.add_todo("番茄钟 UI 测试")
    svc = PomodoroService(st, tick_ms=1000)
    app = web.create_app(st, pomodoro=svc)
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True).start()
    time.sleep(1.3)

    from screen_time_recorder import todo_window
    todo_window.STATE_PATH = os.path.join(tempfile.mkdtemp(), "state.json")
    ui = todo_window.TodoApp()

    def pump(n=4):
        for _ in range(n):
            ui.root.update()
            ui.root.update_idletasks()

    pump()

    assert not ui.pomo_area.winfo_ismapped(), "初始不应显示番茄钟区域"
    assert ui.pomo_btn.cget("text") == "番茄钟"
    print("初始：番茄钟区域隐藏 OK")

    ui.pomo_btn.event_generate("<Button-1>")
    pump()
    assert ui.pomo_area.winfo_ismapped(), "点击后应展开设置面板"
    entries = walk(ui.pomo_area, tk.Entry)
    assert len(entries) == 2, "应有专注/休息两个输入框: %d" % len(entries)
    assert ui.pomo_btn.cget("text") == "收起"
    print("点击番茄钟 → 展开设置面板 OK")

    entries[0].delete(0, "end")
    entries[0].insert(0, "60")
    entries[1].delete(0, "end")
    entries[1].insert(0, "0")
    start = find_label(ui.pomo_area, "开始专注")
    assert start is not None
    start.event_generate("<Button-1>")
    pump()
    s = svc.status()
    assert s["state"] == "focus", s
    assert s["planned_ms"] == 60 * 60000, s
    assert ui._pomo_lbl is not None, "运行条未出现"
    assert ui.pomo_btn.cget("text") == "进行中"
    print("设置 60/0 → 开始专注 OK")

    ui._pomo_action("/api/pomodoro/pause")
    pump()
    assert svc.status()["state"] == "paused"
    assert ui.pomo_btn.cget("text") == "已暂停"
    ui._pomo_action("/api/pomodoro/resume")
    pump()
    assert svc.status()["state"] == "focus"
    print("暂停 / 继续 OK")

    # 非法输入回退
    ui._toggle_pomo_setup()  # 运行中应无效
    pump()
    assert ui.pomo_btn.cget("text") == "进行中"
    print("运行中点击不展开面板 OK")

    ui._pomo_action("/api/pomodoro/stop", {"reason": "test"})
    pump()
    assert svc.status()["state"] == "idle"
    assert not ui.pomo_area.winfo_ismapped(), "结束后应收起"
    assert ui.pomo_btn.cget("text") == "番茄钟"
    print("结束 → 回到空闲 OK")

    # 托盘「自定义时长与休息…」用 settings 标记通知窗口展开
    st.set_setting("pomodoro.open_setup", "1")
    ui._poll_pomo()
    pump()
    assert ui._pomo_setup_open and ui.pomo_area.winfo_ismapped()
    assert st.get_setting("pomodoro.open_setup") is None, "标记应被 ack 清除"
    print("托盘信号 → 自动展开面板 OK")

    # 校验：非法值回退
    entries = walk(ui.pomo_area, tk.Entry)
    entries[0].delete(0, "end")
    entries[0].insert(0, "abc")
    v = ui._pomo_read(ui._focus_var, 5, 180, 25)
    assert v == 25, v
    entries[0].delete(0, "end")
    entries[0].insert(0, "999")
    v = ui._pomo_read(ui._focus_var, 5, 180, 25)
    assert v == 180, v
    print("非法输入回退 / 夹紧 OK")

    ui.on_close()
    st.close()
    print("all pomodoro ui tests passed")


if __name__ == "__main__":
    main()
