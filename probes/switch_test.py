# -*- coding: utf-8 -*-
"""主动切换窗口，验证后台 daemon 能否捕获前台变化。"""
import sys
import io
import time
import ctypes

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import win32gui
import win32process
import psutil

user32 = ctypes.windll.user32

wins = []


def cb(h, _):
    try:
        if win32gui.IsWindowVisible(h) and not win32gui.IsIconic(h):
            t = win32gui.GetWindowText(h)
            _, pid = win32process.GetWindowThreadProcessId(h)
            try:
                proc = psutil.Process(pid).name()
            except Exception:
                proc = "?"
            if t and proc not in ("explorer.exe", "ShellExperienceHost.exe") and len(wins) < 6:
                wins.append((h, proc, t[:18]))
    except Exception:
        pass
    return True


win32gui.EnumWindows(cb, None)
print("候选窗口:", [(w[1], w[2]) for w in wins])

for i in range(4):
    w = wins[i % len(wins)]
    try:
        ok = user32.SetForegroundWindow(w[0])
        print(f"切换到 {w[1]} {w[2]!r} -> SetForegroundWindow={ok}")
    except Exception as e:
        print("切换异常:", e)
    time.sleep(0.6)

time.sleep(1)
print("done")
