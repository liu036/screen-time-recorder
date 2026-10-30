# -*- coding: utf-8 -*-
"""诊断抖音 UIA 抓取：为什么只拿到根窗口名？不抢前台。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psutil
import win32gui
import win32process
import uiautomation as uia

uia.uiautomation.TIME_OUT = 2.0

from screen_time_recorder import douyin_content


def find_douyin():
    procs = set()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if "douyin" in (p.info.get("name") or "").lower():
                procs.add(p.info["pid"])
        except Exception:
            pass
    rows = []

    def cb(h, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if pid in procs and win32gui.IsWindowVisible(h):
                rows.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return rows


def main():
    fg = win32gui.GetForegroundWindow()
    print(f"当前前台: hwnd={fg} title={win32gui.GetWindowText(fg)!r}")
    hs = find_douyin()
    print(f"抖音可见窗口: {hs}")
    if not hs:
        print("⚠️ 无抖音窗口")
        return
    hwnd = fg if fg in hs else hs[0]
    print(f"诊断目标: hwnd={hwnd} 前台={hwnd == fg} 最小化={win32gui.IsIconic(hwnd)}")

    ctrl = uia.ControlFromHandle(hwnd)
    print(f"根控件: Name={ctrl.Name!r} Type={ctrl.ControlTypeName} Class={ctrl.ClassName!r}")
    try:
        children = ctrl.GetChildren()
    except Exception as e:
        print(f"根 GetChildren 失败: {e}")
        return
    print(f"根子控件数: {len(children)}")
    render = None
    for ch in children:
        try:
            print(f"  child: {ch.ControlTypeName:<18} class={str(ch.ClassName)[:34]:<35} "
                  f"name={str(ch.Name)[:40]!r}")
            if "RenderWidget" in str(ch.ClassName):
                render = ch
        except Exception as e:
            print(f"  child 读取失败: {e}")

    rows = []
    counter = {"n": 0, "max": 1500}
    t0 = time.time()
    if render is not None:
        print(">> 从 Chrome_RenderWidgetHostHWND 遍历")
        douyin_content._walk(render, rows, 0, 16, t0 + 8, counter)
    else:
        print(">> 未找到 RenderWidget，回退从根遍历")
        douyin_content._walk(ctrl, rows, 0, 16, t0 + 8, counter)
    print(f"遍历: 节点={counter['n']} 文本={len(rows)} 耗时={time.time()-t0:.2f}s")
    for r in rows[:40]:
        print("   ", r)
    print("extract ->", repr(douyin_content._extract_content(rows)))


if __name__ == "__main__":
    main()
