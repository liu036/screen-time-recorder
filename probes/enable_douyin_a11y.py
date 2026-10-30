# -*- coding: utf-8 -*-
"""点击抖音「开启读屏标签」，并验证内容树是否暴露视频标题/作者。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32gui
import uiautomation as uia

uia.uiautomation.TIME_OUT = 2.0

from screen_time_recorder import douyin_content


def find_button(ctrl, name):
    try:
        if ctrl.ControlTypeName == "ButtonControl" and name in (ctrl.Name or ""):
            return ctrl
    except Exception:
        pass
    try:
        for ch in ctrl.GetChildren():
            r = find_button(ch, name)
            if r is not None:
                return r
    except Exception:
        pass
    return None


def dump_texts(hwnd, limit=60):
    root = uia.ControlFromHandle(hwnd)
    rows = []
    counter = {"n": 0, "max": 2500}
    douyin_content._walk(root, rows, 0, 18, time.time() + 12, counter)
    print(f"节点={counter['n']} 文本={len(rows)}", flush=True)
    for r in rows[:limit]:
        print(f"   {r}", flush=True)
    print(f"extract -> {douyin_content._extract_content(rows)!r}", flush=True)


def main():
    hwnd = int(sys.argv[1]) if len(sys.argv) > 1 else win32gui.GetForegroundWindow()
    print(f"hwnd={hwnd} fg={win32gui.GetForegroundWindow()} title={win32gui.GetWindowText(hwnd)!r}",
          flush=True)
    root = uia.ControlFromHandle(hwnd)
    btn = find_button(root, "开启读屏标签")
    if btn is None:
        print("未找到「开启读屏标签」（可能已开启）", flush=True)
        off = find_button(root, "关闭读屏标签")
        print(f"「关闭读屏标签」按钮: {off!r}", flush=True)
    else:
        print(f"找到按钮: {btn.Name!r} enabled={btn.IsEnabled}", flush=True)
        done = False
        try:
            ip = btn.GetInvokePattern()
            if ip:
                ip.Invoke()
                done = True
                print("InvokePattern.Invoke() 成功", flush=True)
        except Exception as e:
            print(f"Invoke 失败: {e}", flush=True)
        if not done:
            try:
                btn.Click()
                done = True
                print("Click() 成功", flush=True)
            except Exception as e:
                print(f"Click 失败: {e}", flush=True)
        time.sleep(3)

    print("\n===== 开启后内容树 =====", flush=True)
    dump_texts(hwnd)
    print("\n===== 再等 5s 复查（读屏可能异步加载）=====", flush=True)
    time.sleep(5)
    dump_texts(hwnd)


if __name__ == "__main__":
    main()
