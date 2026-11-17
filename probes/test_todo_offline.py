# -*- coding: utf-8 -*-
"""待办窗口离线兜底：主进程没起时不能崩，要显示「连接不上记录器」。

回归背景：_render_offline 曾把 pack 的 pady 元组写进 Label 构造函数，
Tcl 解析成 "28 4" 抛 TclError，导致"主进程没起"这个本该兜底的场景
反而让窗口启动即退出。

运行：python probes/test_todo_offline.py
"""
import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 必须在 import todo_window 之前设置：API_BASE 是模块级常量
os.environ["STR_API"] = "http://127.0.0.1:8799"

from screen_time_recorder import todo_window


def main():
    todo_window.STATE_PATH = os.path.join(tempfile.mkdtemp(), "state.json")
    ui = todo_window.TodoApp()
    try:
        for _ in range(8):
            ui.root.update()
            ui.root.update_idletasks()
        assert ui._offline, "应处于离线状态"
        assert ui.status.cget("text").startswith("离线"), ui.status.cget("text")
        assert len(ui._rows) == 0
        print("主进程未启动 → 显示离线提示、窗口不崩溃 OK")
    finally:
        ui.on_close()
    print("all offline tests passed")


if __name__ == "__main__":
    main()
