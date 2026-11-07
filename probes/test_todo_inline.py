# -*- coding: utf-8 -*-
"""待办窗口单视图交互冒烟：+ 号新建 / 行内编辑 / 优先级循环 / 直接删除。

运行：python probes/test_todo_inline.py
用临时库 + 独立端口，不碰真实数据。
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
from screen_time_recorder import web

PORT = 8768
os.environ["STR_API"] = "http://127.0.0.1:%d" % PORT


def walk(widget, cls):
    found = []
    for c in widget.winfo_children():
        if isinstance(c, cls):
            found.append(c)
        found.extend(walk(c, cls))
    return found


def find_label(parent, text):
    for c in parent.winfo_children():
        if isinstance(c, tk.Label) and c.cget("text") == text:
            return c
        hit = find_label(c, text)
        if hit is not None:
            return hit
    return None


def main():
    st = Storage(os.path.join(tempfile.mkdtemp(), "inline.db"))
    st.add_todo("交周报", priority=2)
    st.add_todo("回复供应商", priority=0)
    app = web.create_app(st)
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True).start()
    time.sleep(1.3)

    from screen_time_recorder import todo_window
    todo_window.STATE_PATH = os.path.join(tempfile.mkdtemp(), "win_state.json")
    ui = todo_window.TodoApp()
    ui.root.update()

    # 1) 没有常驻输入框，只有 + 号按钮
    assert not hasattr(ui, "entry"), "顶部常驻输入框未移除"
    assert ui.add_btn is not None, "+ 号按钮缺失"
    assert not [w for w in ui.root.winfo_children()
                if isinstance(w, tk.Toplevel)], "不应有模态窗口"
    print("无常驻输入框 / + 号按钮存在 / 无模态窗口 OK")

    # 2) 点 + → 列表末尾出现输入框 → 回车写入
    ui._start_new_row()
    ui.root.update()
    entries = walk(ui.inner, tk.Entry)
    assert len(entries) == 1, "新建行未出现输入框: %d" % len(entries)
    e = entries[0]
    e.insert(0, "新待办：验证加号")
    e.event_generate("<Return>")
    ui.root.update()
    titles = [t["title"] for t in st.list_todos("all")]
    assert "新待办：验证加号" in titles, titles
    print("+ 号 → 行内新建 → 回车写入 OK")

    # 3) 单击标题 → 原地变输入框 → 回车保存
    ui.refresh()
    ui.root.update()
    label = find_label(ui.inner, "交周报")
    assert label is not None, "找不到标题 Label"
    item = [t for t in st.list_todos("all") if t["title"] == "交周报"][0]
    ui._start_inline_edit(item, label)
    ui.root.update()
    inline = walk(label, tk.Entry)
    assert len(inline) == 1, "行内输入框未出现"
    assert ui._editing is True
    # 编辑期间刷新不应把输入框重建掉
    before_ids = [str(w) for w in ui.inner.winfo_children()]
    ui.refresh()
    ui.root.update()
    assert [str(w) for w in ui.inner.winfo_children()] == before_ids, \
        "编辑期间轮询重建了列表"
    inline[0].delete(0, "end")
    inline[0].insert(0, "交周报（已改）")
    inline[0].event_generate("<Return>")
    ui.root.update()
    assert ui._editing is False
    assert any(t["title"] == "交周报（已改）" for t in st.list_todos("all"))
    print("单击标题 → 行内编辑 → 回车保存（轮询不打断）OK")

    # 4) 优先级圆点点击循环
    item = [t for t in st.list_todos("all")
            if t["title"] == "交周报（已改）"][0]
    old = item["priority"]
    ui._cycle_priority(item, old)
    ui.root.update()
    new = [t for t in st.list_todos("all") if t["id"] == item["id"]][0]
    assert new["priority"] == (old + 1) % 3, (old, new["priority"])
    print("优先级循环 %d → %d OK" % (old, new["priority"]))

    # 5) 点 × 直接删除，不弹确认框
    victim = [t for t in st.list_todos("all")
              if t["title"] == "回复供应商"][0]
    before = len(st.list_todos("all"))
    ui.on_delete(victim)
    ui.root.update()
    assert len(st.list_todos("all")) == before - 1, "删除未生效"
    assert not [w for w in ui.root.winfo_children()
                if isinstance(w, tk.Toplevel)], "删除不应产生对话框"
    print("点 × 直接删除（无确认弹窗）OK")

    ui.on_close()
    st.close()
    print("all todo inline tests passed")


if __name__ == "__main__":
    main()
