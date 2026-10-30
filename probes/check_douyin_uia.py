# -*- coding: utf-8 -*-
"""检测当前前台 + 尝试对抖音窗口做 UIA 抓取（不抢前台）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psutil
import win32gui
import win32process

from screen_time_recorder import douyin_content


def fg_info():
    h = win32gui.GetForegroundWindow()
    try:
        _, pid = win32process.GetWindowThreadProcessId(h)
        name = psutil.Process(pid).name()
    except Exception:
        name = "?"
    return h, name, win32gui.GetWindowText(h)


def douyin_windows():
    procs = set()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            n = p.info.get("name") or ""
            if "douyin" in n.lower():
                procs.add(p.info["pid"])
        except Exception:
            pass
    rows = []

    def cb(h, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if pid in procs and win32gui.IsWindowVisible(h):
                rows.append({
                    "hwnd": h,
                    "title": win32gui.GetWindowText(h),
                    "cls": win32gui.GetClassName(h),
                    "iconic": bool(win32gui.IsIconic(h)),
                })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return rows


def main():
    h, name, title = fg_info()
    print(f"当前前台: hwnd={h} proc={name!r} title={title!r}")
    ws = douyin_windows()
    print(f"抖音可见窗口: {len(ws)}")
    for w in ws:
        print(f"  hwnd={w['hwnd']} iconic={w['iconic']} cls={w['cls']} title={w['title']!r}")
    if not ws:
        print("⚠️ 未找到可见抖音窗口")
        return
    for w in ws:
        print(f"\n尝试抓取 hwnd={w['hwnd']}（不抢前台）...")
        c = douyin_content.fetch_douyin_content(w["hwnd"], timeout=6)
        print(f"  -> content = {c!r}")


if __name__ == "__main__":
    main()
