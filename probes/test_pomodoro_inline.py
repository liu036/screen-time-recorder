# -*- coding: utf-8 -*-
"""番茄钟 P1 交互探针：待办行内「专注」+ 循环 + 提醒弹窗截图。

临时库 + 独立端口，不碰真实数据；弹窗用 pythonw 真进程拉起后截图。
"""
import ctypes
import os
import subprocess
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


def work_area():
    from ctypes import wintypes
    r = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


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
    assert s["state"] == "focus", s
    assert s["todo_id"] == expect_id, s
    assert s["title"] == expect_title, s
    assert ui._row_pomo_lbl is not None, "专注中的行应显示倒计时"
    assert ":" in ui._row_pomo_lbl.cget("text"), ui._row_pomo_lbl.cget("text")
    print("点「专注」→ 绑定待办并开始 / 行内倒计时 OK")

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
    assert "休息 5 分 → 继续" in joined, joined
    assert "直接下一轮" in joined, joined
    print("结束面板：休息并继续 / 直接下一轮 / 结束 OK")

    # 截图 todo 窗口（含行内专注与结束面板）
    ui.root.update()
    x, y = ui.root.winfo_rootx(), ui.root.winfo_rooty()
    w, h = ui.root.winfo_width(), ui.root.winfo_height()
    shot("_pomo_inline.png", (x, y, x + w, y + h))

    # 拉起真实提醒弹窗并截图
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            exe = cand
    env = os.environ.copy()
    env["STR_API"] = API
    proc = subprocess.Popen(
        [exe, "-m", "screen_time_recorder.pomodoro_popup",
         "--kind", "focus_done"],
        cwd=BASE_DIR, env=env, creationflags=0x00000008)
    time.sleep(2.0)
    left, top, right, bottom = work_area()
    shot("_pomo_popup.png", (right - 420, bottom - 240, right, bottom))
    proc.terminate()

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

    # 休息结束轻提示（此时下一轮已在跑，文案应读当前会话）
    proc = subprocess.Popen(
        [exe, "-m", "screen_time_recorder.pomodoro_popup",
         "--kind", "break_done"],
        cwd=BASE_DIR, env=env, creationflags=0x00000008)
    time.sleep(2.0)
    left, top, right, bottom = work_area()
    shot("_pomo_popup_break.png", (right - 420, bottom - 240, right, bottom))
    proc.terminate()

    svc.stop("test")
    ui.on_close()
    st.close()
    print("all pomodoro inline tests passed")


if __name__ == "__main__":
    main()
