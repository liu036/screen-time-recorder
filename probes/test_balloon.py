# -*- coding: utf-8 -*-
"""托盘气泡冒烟测试：真实弹出一条通知，5 秒后清理临时图标。

运行：python probes/test_balloon.py
"""
import os
import sys
import time

import win32api
import win32con
import win32gui

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.tray import _balloon, build_ico, WM_TRAYICON


def main():
    ico = build_ico()
    hinst = win32api.GetModuleHandle(None)
    cls = "STRBalloonTest"
    wc = win32gui.WNDCLASS()
    wc.hInstance = hinst
    wc.lpszClassName = cls
    wc.lpfnWndProc = win32gui.DefWindowProc
    try:
        win32gui.RegisterClass(wc)
    except win32gui.error:
        pass
    hwnd = win32gui.CreateWindow(cls, "balloon test", 0,
                                 0, 0, 0, 0, 0, 0, hinst, None)
    hicon = win32gui.LoadImage(0, ico, win32con.IMAGE_ICON, 0, 0,
                               win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
    nid = (hwnd, 0,
           win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
           WM_TRAYICON, hicon, "气泡测试")
    win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
    time.sleep(0.4)

    ok = _balloon(hwnd, hicon, "气泡测试", "待办提醒 · 测试",
                  "这是一条测试提醒：交周报 · 今天 18:00\n（5 秒后自动清理）")
    print("Shell_NotifyIcon 返回:", ok)
    time.sleep(5)
    win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
    win32gui.DestroyWindow(hwnd)
    try:
        os.remove(ico)
    except Exception:
        pass
    print("清理完成")


if __name__ == "__main__":
    main()
