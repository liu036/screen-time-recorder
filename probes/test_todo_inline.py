# -*- coding: utf-8 -*-
"""待办窗口单视图交互冒烟（12 项）：+ 号新建 / 行内编辑 / 优先级循环 /
直接删除 / 滚动开关 / 点空白保存 / 真实点击按钮与标题不被全局回调吃掉。

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

    # 优先级三态循环（回归：曾因 `current or 1` 退化成灰/高两态）
    assert [todo_window.next_priority(i) for i in (0, 1, 2)] == [1, 2, 0]
    assert todo_window.PRIO_COLORS[2] == "#C0553B", "最高级应为红色"
    print("优先级三态循环 0→1→2→0 / 最高级红色 OK")

    ui = todo_window.TodoApp()
    ui.root.update()

    def wait_for(pred, timeout=3.0):
        """等待后台写线程 + Tk 回调完成。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            ui.root.update()
            if pred():
                return True
            time.sleep(0.02)
        return False

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
    assert wait_for(lambda: any(
        t["title"] == "新待办：验证加号" for t in st.list_todos("all"))), \
        st.list_todos("all")
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
    assert ui._editing is False
    assert wait_for(lambda: any(
        t["title"] == "交周报（已改）" for t in st.list_todos("all")))
    print("单击标题 → 行内编辑 → 回车保存（轮询不打断）OK")

    # 4) 优先级圆点点击循环
    item = [t for t in st.list_todos("all")
            if t["title"] == "交周报（已改）"][0]
    old = item["priority"]
    expected = todo_window.next_priority(old)
    ui._cycle_priority(item, None, None)   # 本用例不校验圆点重绘
    assert wait_for(lambda: [t for t in st.list_todos("all")
                             if t["id"] == item["id"]][0]["priority"]
                    == expected)
    print("优先级循环 %d → %d OK" % (old, expected))

    # 5) 点 × 直接删除，不弹确认框
    victim = [t for t in st.list_todos("all")
              if t["title"] == "回复供应商"][0]
    before = len(st.list_todos("all"))
    ui.on_delete(victim)
    assert wait_for(lambda: len(st.list_todos("all")) == before - 1), \
        "删除未生效"
    assert not [w for w in ui.root.winfo_children()
                if isinstance(w, tk.Toplevel)], "删除不应产生对话框"
    print("点 × 直接删除（无确认弹窗）OK")

    def pump(n=3):
        for _ in range(n):
            ui.root.update()
            ui.root.update_idletasks()

    class Wheel:
        delta = -120

    # 6) 内容不足：不预设滚动区，滚动条不显示、滚轮无效
    ui.refresh()
    pump()
    assert not ui.scroll.winfo_ismapped(), "内容不足时滚动条仍在"
    assert ui._scrollable is False
    sr = str(ui.canvas.cget("scrollregion")).split()
    assert int(sr[3]) >= ui.canvas.winfo_height(), sr
    view_before = ui.canvas.yview()
    ui._on_wheel(Wheel())
    pump()
    assert ui.canvas.yview() == view_before, "内容不足时不应能滚动"
    print("内容不足：无滚动条 / 滚轮无效 OK")

    # 7) 内容溢出：滚动条才出现，滚轮生效
    for i in range(24):
        st.add_todo("压测待办 %02d" % i)
    ui.refresh()
    pump()
    assert ui.scroll.winfo_ismapped(), "内容溢出时滚动条未出现"
    assert ui._scrollable is True
    ui.canvas.yview_moveto(0.0)
    ui._on_wheel(Wheel())
    pump()
    assert ui.canvas.yview()[0] > 0, "溢出时滚轮应能下滚"
    print("内容溢出：滚动条出现 / 滚轮生效 OK")

    # 8) 点击任意空白自动保存；点输入框自身不提交
    ui.canvas.yview_moveto(0.0)
    pump()
    label = find_label(ui.inner, "交周报（已改）")
    assert label is not None
    item = [t for t in st.list_todos("all")
            if t["title"] == "交周报（已改）"][0]
    ui._start_inline_edit(item, label)
    pump()
    e = walk(label, tk.Entry)[0]
    e.delete(0, "end")
    e.insert(0, "点空白自动保存")
    e.event_generate("<Button-1>", x=5, y=5)
    pump()
    assert ui._editing is True, "点输入框自身不应提交"
    ui.canvas.event_generate("<Button-1>", x=20, y=300)
    pump()
    assert ui._editing is False, "点空白未结束编辑"
    assert wait_for(lambda: any(t["title"] == "点空白自动保存"
                                for t in st.list_todos("all")))
    print("点击任意空白自动保存（行内编辑）OK")

    # 9) 新建行点空白同样自动保存
    ui._start_new_row()
    pump()
    entries = walk(ui.inner, tk.Entry)
    assert len(entries) == 1, entries
    entries[0].insert(0, "新建行点空白保存")
    ui.canvas.event_generate("<Button-1>", x=20, y=300)
    pump()
    assert ui._editing is False
    assert wait_for(lambda: any(t["title"] == "新建行点空白保存"
                                for t in st.list_todos("all")))
    print("点击任意空白自动保存（新建行）OK")

    # 10) 编辑中点筛选：先提交输入再切换，界面与数据一致
    label = find_label(ui.inner, "点空白自动保存")
    assert label is not None
    item = [t for t in st.list_todos("all")
            if t["title"] == "点空白自动保存"][0]
    ui._start_inline_edit(item, label)
    pump()
    e = walk(label, tk.Entry)[0]
    e.delete(0, "end")
    e.insert(0, "切筛选前自动保存")
    ui.tab_btns["open"].event_generate("<Button-1>")
    pump()
    assert ui.filter == "open", ui.filter
    assert ui._editing is False
    assert wait_for(lambda: any(t["title"] == "切筛选前自动保存"
                                for t in st.list_todos("all")))
    print("编辑中点筛选：先保存再切换 OK")

    # 11) 回归：真实点击 ＋ 号（同一次点击不能被全局"点空白"处理吃掉）
    ui.filter = "all"
    ui._last_sig = None
    ui.refresh()
    pump()
    ui.add_btn.event_generate("<Button-1>", x=17, y=17)
    pump()
    assert ui._editing is True, "点 ＋ 号后应进入新建态"
    entries = walk(ui.inner, tk.Entry)
    assert len(entries) == 1, "点 ＋ 号后应有 1 个输入框: %d" % len(entries)
    entries[0].insert(0, "真实点击加号")
    ui.canvas.event_generate("<Button-1>", x=20, y=300)
    pump()
    assert ui._editing is False
    assert wait_for(lambda: any(t["title"] == "真实点击加号"
                                for t in st.list_todos("all")))
    print("真实点击 ＋ 号 → 输入框出现 → 点空白保存 OK")

    # 12) 回归：真实点击标题（行内编辑不能被同一次点击立刻提交）
    ui._last_sig = None
    ui.refresh()
    pump()
    label = find_label(ui.inner, "真实点击加号")
    assert label is not None, "新建的行未渲染"
    label.event_generate("<Button-1>", x=10, y=10)
    pump()
    assert ui._editing is True, "点标题后应进入编辑态"
    inline = walk(label, tk.Entry)
    assert len(inline) == 1, "点标题后应有 1 个行内输入框: %d" % len(inline)
    inline[0].delete(0, "end")
    inline[0].insert(0, "真实点击标题改")
    ui.canvas.event_generate("<Button-1>", x=20, y=300)
    pump()
    assert ui._editing is False
    assert wait_for(lambda: any(t["title"] == "真实点击标题改"
                                for t in st.list_todos("all")))
    print("真实点击标题 → 行内编辑 → 点空白保存 OK")

    ui.on_close()
    st.close()
    print("all todo inline tests passed")


if __name__ == "__main__":
    main()
