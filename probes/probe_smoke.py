# -*- coding: utf-8 -*-
r"""
T2 冒烟探针 —— 验证基础窗口采集能力

用途：确认 pywin32 环境可用，并一次性摸清这台机器上"能拿到什么"。
顺带预热 T6（可见窗口枚举）与 T7（空闲检测）。

用法（必须在你自己的桌面会话里运行，不要在服务/远程会话里跑）：
    chcp 65001
    .venv\Scripts\python.exe probes\probe_smoke.py
"""

import ctypes
import platform
import sys
from datetime import datetime

# 中文窗口标题在 GBK 控制台会炸，强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import psutil
    import win32api
    import win32gui
    import win32process
except ImportError as e:
    print("依赖未安装:", e)
    print("请先运行: .venv\\Scripts\\python.exe -m pip install pywin32 psutil uiautomation")
    sys.exit(1)


def hr(title=""):
    print("\n" + "=" * 64)
    if title:
        print(title)
        print("=" * 64)


def get_window_info(hwnd):
    """尽量全地取一个窗口的信息，任何一项失败都不影响其它项"""
    info = {"hwnd": hwnd}

    try:
        info["title"] = win32gui.GetWindowText(hwnd)
    except Exception as e:
        info["title"] = f"<err {e}>"

    try:
        info["class"] = win32gui.GetClassName(hwnd)
    except Exception as e:
        info["class"] = f"<err {e}>"

    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        info["pid"] = pid
        try:
            p = psutil.Process(pid)
            info["process_name"] = p.name()
            info["exe_path"] = p.exe()
            info["proc_create"] = datetime.fromtimestamp(
                p.create_time()
            ).isoformat(timespec="seconds")
        except Exception as e:
            info["process_name"] = f"<err {e}>"
    except Exception as e:
        info["pid"] = f"<err {e}>"

    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        info["rect"] = [left, top, right, bottom]
        info["size"] = [right - left, bottom - top]
    except Exception as e:
        info["rect"] = f"<err {e}>"

    try:
        info["visible"] = bool(win32gui.IsWindowVisible(hwnd))
        info["iconic"] = bool(win32gui.IsIconic(hwnd))
    except Exception as e:
        info["visible"] = f"<err {e}>"

    try:
        monitor = win32api.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        info["monitor_handle"] = int(monitor) if monitor else None
    except Exception as e:
        info["monitor_handle"] = f"<err {e}>"

    return info


def show(info, keys=None):
    keys = keys or list(info.keys())
    for k in keys:
        print(f"  {k:<15}: {info.get(k)}")


def main():
    hr("1. 系统信息")
    print("  平台        :", platform.platform())
    print("  版本        :", platform.version())
    print("  Python      :", sys.version.split()[0])
    print("  psutil      :", psutil.__version__)
    try:
        monitors = win32api.EnumDisplayMonitors()
        print("  显示器数量  :", len(monitors))
        for i, m in enumerate(monitors):
            print(f"    显示器{i} 矩形: {m[2]}")
    except Exception as e:
        print("  显示器枚举失败:", e)

    hr("2. 当前前台窗口（核心能力）")
    hwnd = win32gui.GetForegroundWindow()
    print("  前台 hwnd   :", hwnd)
    if hwnd:
        show(get_window_info(hwnd))

    hr("3. 空闲检测 GetLastInputInfo（T7 预热）")

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]

    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        ok = ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        tick = ctypes.windll.kernel32.GetTickCount()
        print("  调用成功    :", bool(ok))
        print(f"  系统空闲    : {(tick - lii.dwTime) / 1000:.1f} 秒")
    except Exception as e:
        print("  失败:", e)

    hr("4. 可见窗口枚举（T6 预热）")
    handles = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h).strip():
                handles.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    print("  可见且有标题的窗口数:", len(handles))
    print(f"  {'PID':<8} {'进程名':<26} {'hwnd':<12} 标题")
    print("  " + "-" * 88)
    for h in handles[:25]:
        i = get_window_info(h)
        title = str(i.get("title", ""))[:52]
        print(
            f"  {str(i.get('pid','')):<8} "
            f"{str(i.get('process_name','?')):<26} "
            f"{str(i.get('hwnd')):<12} {title}"
        )

    hr("冒烟完成")
    print("  请把以上完整输出贴回来。")
    print("  重点看：① 前台窗口信息是否拿到了进程名和 exe 路径")
    print("          ② 可见窗口列表里有没有不该出现的窗口")
    print("          ③ 中文标题是否显示正常")


if __name__ == "__main__":
    main()
