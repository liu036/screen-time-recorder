# -*- coding: utf-8 -*-
"""抖音在前台时，循环重试 UIA 抓取，观察 accessibility 树是否随时间构建。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32gui
import uiautomation as uia

uia.uiautomation.TIME_OUT = 2.0

from screen_time_recorder import douyin_content

HWND = int(sys.argv[1]) if len(sys.argv) > 1 else 0


def once(hwnd):
    ctrl = uia.ControlFromHandle(hwnd)
    chs = ctrl.GetChildren()
    render = None
    info = []
    for ch in chs:
        try:
            cname = str(ch.ClassName)
            info.append((ch.ControlTypeName, cname[:28], str(ch.Name)[:28]))
            if "RenderWidget" in cname:
                render = ch
        except Exception:
            pass
    rows = []
    c = {"n": 0, "max": 1500}
    t0 = time.time()
    if render is not None:
        douyin_content._walk(render, rows, 0, 16, t0 + 6, c)
    else:
        for ch in chs:
            try:
                douyin_content._walk(ch, rows, 0, 12, time.time() + 3, c)
            except Exception:
                pass
    return len(chs), render is not None, c["n"], len(rows), info, douyin_content._extract_content(rows)


def main():
    hwnd = HWND or win32gui.GetForegroundWindow()
    print(f"目标 hwnd={hwnd} title={win32gui.GetWindowText(hwnd)!r}", flush=True)
    for i in range(1, 21):
        fg = win32gui.GetForegroundWindow()
        if fg != hwnd:
            print(f"[{i:02d}] 前台已变为 {fg}（非目标），停止", flush=True)
            break
        try:
            # 已是前台时再次置前台，无副作用；但会触发 Chromium 焦点事件
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.3)
        try:
            nch, has_rw, nn, nt, info, ext = once(hwnd)
            print(f"[{i:02d}] children={nch} RenderWidget={has_rw} nodes={nn} text={nt} "
                  f"extract={ext!r}", flush=True)
            if i in (1, 5, 10, 15, 20):
                for x in info:
                    print(f"      {x}", flush=True)
        except Exception as e:
            print(f"[{i:02d}] err {e}", flush=True)
        time.sleep(0.5)
    print("done", flush=True)


if __name__ == "__main__":
    main()
