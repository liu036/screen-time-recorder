# -*- coding: utf-8 -*-
"""番茄钟 P1 交互探针：待办行内「专注」+ 循环 + 提醒弹窗截图。

临时库 + 独立端口，不碰真实数据；弹窗用 pythonw 真进程拉起后截图。
"""
import ctypes
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

PORT = 8771
API = "http://127.0.0.1:%d" % PORT
os.environ["STR_API"] = API
SHOT_DIR = os.path.join(BASE_DIR, "probes")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


def pump(ui, n=6):
    for _ in range(n):
        ui.root.update()
        ui.root.update_idletasks()


def rows(ui):
    out = []
    for outer in ui.inner.winfo_children():
        if not isinstance(outer, tk.Frame):
            continue
        for row in outer.winfo_children():
            if isinstance(row, tk.Frame) and row.winfo_children():
                out.append(row)
    return out


def labels(row):
    return [w for w in row.winfo_children() if isinstance(w, tk.Label)]


def all_texts(widget):
    out = []
    for c in widget.winfo_children():
        if isinstance(c, tk.Label):
            out.append(c.cget("text"))
        out.extend(all_texts(c))
    return out


def find_label(widget, text):
    for c in widget.winfo_children():
        if isinstance(c, tk.Label) and c.cget("text") == text:
            return c
        hit = find_label(c, text)
        if hit is not None:
            return hit
    return None


def find_focus_btn(ui):
    for row in rows(ui):
        for w in labels(row):
            if w.cget("text") in ("专注",) or ":" in w.cget("text"):
                if w.cget("cursor") == "hand2":
                    return row, w
    return None, None


def shot(name, region):
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=region)
        path = os.path.join(SHOT_DIR, name)
        img.save(path)
        print("截图:", path)
        return path
    except Exception as e:
        print("截图失败:", e)
        return None


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    st = Storage(os.path.join(tempfile.mkdtemp(), "pinline.db"))
    t1 = st.add_todo("写这周的复盘")
    t2 = st.add_todo("整理唛头打印需求")
    clock = Clock()
    svc = PomodoroService(st, clock=clock,
                          wall_ms=lambda: int(time.time() * 1000),
                          tick_ms=1000)
    app = web.create_app(st, pomodoro=svc)
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True).start()
    time.sleep(1.2)

    from screen_time_recorder import todo_window
    todo_window.STATE_PATH = os.path.join(tempfile.mkdtemp(), "state.json")
    ui = todo_window.TodoApp()
    pump(ui)

    row, btn = find_focus_btn(ui)
    assert btn is not None, "每行应有「专注」入口"
    assert btn.cget("text") == "专注", btn.cget("text")
    print("行内专注入口存在 OK")

    title_text = [w.cget("text") for w in labels(row)
                  if w.cget("text") in ("写这周的复盘", "整理唛头打印需求")]
    assert title_text, [w.cget("text") for w in labels(row)]
    expect_title = title_text[0]
    expect_id = t1 if expect_title == "写这周的复盘" else t2

    btn.event_generate("<Button-1>")
    pump(ui, 10)
    s = svc.status()
    assert s["state"] == "idle", "行内「专注」只应唤起设置，不应直接开始"
    assert ui._pomo_setup_open and ui.pomo_area.winfo_ismapped()
    assert ui._pomo_todo and ui._pomo_todo["id"] == expect_id
    texts = all_texts(ui.pomo_area)
    assert any("为「" in t for t in texts), texts
    ui.root.update()
    x, y = ui.root.winfo_rootx(), ui.root.winfo_rooty()
    w, h = ui.root.winfo_width(), ui.root.winfo_height()
    shot("_pomo_setup.png", (x, y, x + w, y + h))
    print("点「专注」→ 唤起时长设置（未开始）OK")

    start = find_label(ui.pomo_area, "开始专注")
    assert start is not None, all_texts(ui.pomo_area)
    start.event_generate("<Button-1>")
    pump(ui, 10)
    s = svc.status()
    assert s["state"] == "focus", s
    assert s["todo_id"] == expect_id, s
    assert s["title"] == expect_title, s
    assert ui._row_pomo_lbl is not None, "专注中的行应显示倒计时"
    assert ":" in ui._row_pomo_lbl.cget("text"), ui._row_pomo_lbl.cget("text")
    print("确认开始 → 绑定待办 / 行内倒计时 OK")

    # 把时间推到结束
    clock.advance(25 * 60)
    svc.tick()
    assert svc.status()["state"] == "focus_done", svc.status()
    ui._poll_pomo()
    pump(ui, 10)
    texts = []
    for w in ui.pomo_area.winfo_children():
        texts.extend([c.cget("text") for c in w.winfo_children()
                      if isinstance(c, tk.Label)])
        for c in w.winfo_children():
            if isinstance(c, tk.Frame):
                texts.extend([x.cget("text") for x in c.winfo_children()
                              if isinstance(x, tk.Label)])
    joined = " | ".join(texts)
    assert "休息 5 分后继续" in joined, joined
    assert "只休息 5 分" in joined, joined
    assert "结束" in joined, joined
    print("结束面板：休息后继续 / 只休息 / 结束 OK")

    # 截图 todo 窗口（含行内专注与结束面板）
    ui.root.update()
    x, y = ui.root.winfo_rootx(), ui.root.winfo_rooty()
    w, h = ui.root.winfo_width(), ui.root.winfo_height()
    shot("_pomo_inline.png", (x, y, x + w, y + h))

    # 点「休息并继续」→ 休息 → 自动下一轮
    svc.start_break(chain=True)
    assert svc.status()["state"] == "break"
    ui._poll_pomo()
    pump(ui, 8)
    texts = all_texts(ui.pomo_area)
    assert any("休息中" in t for t in texts), texts
    print("休息态面板渲染 OK")

    clock.advance(5 * 60)
    svc.tick()
    s = svc.status()
    assert s["state"] == "focus", s
    assert s["todo_id"] == expect_id and s["title"] == expect_title, s
    print("休息结束 → 自动接下一轮 / 继承待办 OK")

    # 专注中点另一条待办：确认后中断旧会话，并进入新任务的时长设置
    other_row = None
    for r in rows(ui):
        texts_r = [w.cget("text") for w in labels(r)]
        if expect_title not in texts_r and "专注" in texts_r:
            other_row = r
            break
    assert other_row is not None, "找不到另一条待办"

    import screen_time_recorder.pomodoro_panel as panel
    old_ask = panel.messagebox.askyesno
    panel.messagebox.askyesno = lambda *a, **k: True
    try:
        [w for w in labels(other_row)
         if w.cget("text") == "专注"][0].event_generate("<Button-1>")
        pump(ui, 10)
    finally:
        panel.messagebox.askyesno = old_ask

    s = svc.status()
    assert s["state"] == "idle", s
    assert ui._pomo_todo and ui._pomo_todo["id"] != expect_id, ui._pomo_todo
    assert ui._pomo_setup_open
    print("专注中切换任务 → 中断旧会话并进入新设置 OK")

    svc.stop("test")
    ui.on_close()
    st.close()
    print("all pomodoro inline tests passed")


if __name__ == "__main__":
    main()
