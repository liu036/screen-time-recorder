# -*- coding: utf-8 -*-
"""待办窗口冒烟测试：临时库 + 独立端口，窗口显示 3 秒并截图。

运行：python probes/test_todo_window.py
截图输出：probes/_todo_window.png
"""
import os
import sys
import tempfile
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from screen_time_recorder.storage import Storage
from screen_time_recorder import web

PORT = 8768
os.environ["STR_API"] = "http://127.0.0.1:%d" % PORT


def main():
    db = os.path.join(tempfile.mkdtemp(), "win.db")
    st = Storage(db)
    now = int(time.time() * 1000)
    st.add_todo("交周报：本周采集与待办进展", remind_ts=now + 3600000, priority=2)
    st.add_todo("回复供应商邮件（唛头模板确认）", due_ts=now - 86400000)
    st.add_todo("看 Barcode 优化的两个待改点", priority=1)
    st.add_todo("整理入职培训笔记", priority=0)

    app = web.create_app(st)
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True).start()
    time.sleep(1.3)

    from screen_time_recorder import todo_window
    # 隔离窗口状态文件，避免覆盖真实几何信息
    todo_window.STATE_PATH = os.path.join(tempfile.mkdtemp(), "win_state.json")
    ui = todo_window.TodoApp()
    # 截图期间移到干净区域并临时置顶，避免被桌面悬浮窗/桌宠挡住
    ui.root.geometry("360x520+120+120")
    ui.root.attributes("-topmost", True)
    ui.root.lift()
    ui.filter = "today"
    ui.refresh()

    result = {}

    def shot():
        try:
            from PIL import ImageGrab
            ui.root.lift()
            ui.root.update_idletasks()
            time.sleep(0.4)
            x, y = ui.root.winfo_rootx(), ui.root.winfo_rooty()
            w, h = ui.root.winfo_width(), ui.root.winfo_height()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            path = os.path.join(BASE_DIR, "probes", "_todo_window.png")
            img.save(path)
            result["shot"] = path
            result["size"] = (w, h)
            result["rows"] = len(ui._rows)
            result["title"] = ui.root.title()
            print("窗口标题:", ui.root.title(), "| 尺寸:", (w, h),
                  "| 渲染行数:", len(ui._rows), "| 截图:", path)
        except Exception as e:
            result["error"] = str(e)
            print("截图失败:", e)

    # 再切到"全部"确认已完成项也能渲染
    ui.root.after(1200, shot)
    ui.root.after(3000, ui.on_close)
    ui.run()

    st.close()
    assert result.get("rows") == 4, result
    assert not result.get("error"), result
    print("todo window smoke passed")


if __name__ == "__main__":
    main()
