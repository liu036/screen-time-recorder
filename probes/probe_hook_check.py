# -*- coding: utf-8 -*-
"""
T3 专项：验证 SetWinEventHook(EVENT_SYSTEM_FOREGROUND) 能否稳定捕获
自动在后台切换两次前台窗口（约 1.5 秒），不写文件，只打印结果。
"""
import ctypes
import io
import sys
import time
import threading
from ctypes import wintypes

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import win32gui
import win32process

user32 = ctypes.windll.user32
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
PM_REMOVE = 0x0001

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
)

events = []
lock = threading.Lock()


cb_hits = []


def on_fg(hook, event, hwnd, idObject, idChild, thread, ts):
    cb_hits.append((event, hwnd, idObject, idChild))
    try:
        title = win32gui.GetWindowText(hwnd) if hwnd else ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        title, pid = "", 0
    with lock:
        events.append({
            "t": time.time(),
            "hwnd": hwnd,
            "pid": pid,
            "title": title[:60],
            "ms": ts,
        })


def pname(pid):
    import psutil
    try:
        return psutil.Process(pid).name()
    except Exception:
        return "?"


def main():
    cb = WINEVENTPROC(on_fg)
    stop = threading.Event()
    hook_box = {}

    def loop():
        # 关键：注册钩子与消息循环必须在同一个线程
        h = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
                                   0, cb, 0, 0,
                                   WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        hook_box["h"] = h
        hook_box["tid"] = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        while not stop.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.02)
        if h:
            user32.UnhookWinEvent(wintypes.HANDLE(h))

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    time.sleep(0.6)
    hook = hook_box.get("h")
    print(f"SetWinEventHook -> {hook}  (注册线程 {hook_box.get('tid')}, "
          f"主线程 {ctypes.windll.kernel32.GetCurrentThreadId()})")
    if not hook:
        print("❌ 钩子注册失败")
        return

    # 选两个不同的窗口来切换
    cands = []

    def cb2(h, _):
        try:
            if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
                return True
            t = win32gui.GetWindowText(h)
            if t.strip() and t not in ("Program Manager",):
                cands.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb2, None)
    cands = cands[:5]
    print(f"候选窗口: {cands}")
    print("当前前台:", win32gui.GetForegroundWindow())

    for h in cands[:3]:
        before = win32gui.GetForegroundWindow()
        r1 = user32.SetForegroundWindow(wintypes.HWND(h))
        time.sleep(0.35)
        after = win32gui.GetForegroundWindow()
        print(f"  切换 {h}: SetForegroundWindow={r1}  前台 {before} -> {after}"
              f"  {'✅变化' if before != after else '❌未变'}")
        time.sleep(0.15)

    time.sleep(0.5)
    stop.set()
    th.join(timeout=2)

    print()
    print(f"回调被调起次数（原始）: {len(cb_hits)}")
    for c in cb_hits[:10]:
        print("   raw:", c)
    print(f"=== 捕获到 {len(events)} 条前台事件 ===")
    for e in events:
        print(f"  hwnd={e['hwnd']:<9} pid={e['pid']:<7} {pname(e['pid'])[:18]:<19} {e['title']!r}")
    print()
    print("✅ 事件驱动可用" if events else "❌ 一个事件都没捕获到")


if __name__ == "__main__":
    main()
