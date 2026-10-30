# -*- coding: utf-8 -*-
"""等待抖音成为前台，自动诊断 UIA 树；对比主线程与子线程（模拟 ContentFetcher）。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psutil
import win32gui
import win32process
import uiautomation as uia

uia.uiautomation.TIME_OUT = 2.0

from screen_time_recorder import douyin_content


def douyin_windows():
    procs = set()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if "douyin" in (p.info.get("name") or "").lower():
                procs.add(p.info["pid"])
        except Exception:
            pass
    out = []

    def cb(h, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if pid in procs and win32gui.IsWindowVisible(h):
                out.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def diag(hwnd, label):
    print(f"--- {label} ---", flush=True)
    ctrl = uia.ControlFromHandle(hwnd)
    print(f"root: {ctrl.Name!r} {ctrl.ControlTypeName} {ctrl.ClassName!r}", flush=True)
    chs = ctrl.GetChildren()
    print(f"children: {len(chs)}", flush=True)
    render = None
    for ch in chs:
        try:
            print(f"   {ch.ControlTypeName:<18} class={str(ch.ClassName)[:34]:<35} "
                  f"name={str(ch.Name)[:50]!r}", flush=True)
            if "RenderWidget" in str(ch.ClassName):
                render = ch
        except Exception as e:
            print(f"   err {e}", flush=True)
    rows = []
    c = {"n": 0, "max": 1500}
    t0 = time.time()
    if render is not None:
        douyin_content._walk(render, rows, 0, 16, t0 + 10, c)
    else:
        douyin_content._walk(ctrl, rows, 0, 16, t0 + 10, c)
    print(f"nodes={c['n']} text={len(rows)} {time.time()-t0:.2f}s", flush=True)
    for r in rows[:25]:
        print(f"    {r}", flush=True)
    print(f"extract -> {douyin_content._extract_content(rows)!r}", flush=True)


def main():
    deadline = time.time() + 300
    print("等待抖音成为前台（最多 300s）...", flush=True)
    while time.time() < deadline:
        fg = win32gui.GetForegroundWindow()
        ws = douyin_windows()
        if fg in ws:
            print(f"检测到抖音前台 hwnd={fg}，等 2s 渲染后诊断", flush=True)
            time.sleep(2)
            diag(fg, "主线程")
            res = {}

            def sub():
                res["v"] = douyin_content.fetch_douyin_content(fg, timeout=10)

            t = threading.Thread(target=sub)
            t.start()
            t.join(25)
            print(f"子线程 fetch -> {res.get('v')!r}", flush=True)
            return
        time.sleep(0.5)
    print("超时：未检测到抖音前台", flush=True)


if __name__ == "__main__":
    main()
