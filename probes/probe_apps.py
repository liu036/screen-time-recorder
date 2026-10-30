# -*- coding: utf-8 -*-
r"""
T5 应用识别探针 —— UWP 解析 + 噪音窗口特征

验证三件事：
1. UWP 应用（计算器/设置/照片等）能否从 ApplicationFrameHost.exe 解析出真实应用名
2. GetApplicationUserModelId 能否拿到 AUMID（应用唯一标识）
3. 采集系统噪音窗口特征，为过滤规则做准备

流程：启动若干 UWP 应用 → 采集 → 关闭并恢复原前台窗口
"""

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import psutil
import win32con
import win32gui
import win32process

# 待测 UWP 应用（名称, URI）
UWP_APPS = [
    ("计算器", "calculator:"),
    ("设置", "ms-settings:"),
    ("照片", "ms-photos:"),
    ("时钟", "ms-clock:"),
    ("应用商店", "ms-windows-store:"),
]

# 已知噪音进程（待验证）
NOISE_HINTS = [
    "explorer", "TextInputHost", "NVIDIA", "ShellExperienceHost",
    "Shell_Experience", "StartMenuExperienceHost", "SearchHost",
    "SystemSettings", "LockApp", "ApplicationFrameHost",
]


def hr(t=""):
    print("\n" + "=" * 72)
    if t:
        print(t)
        print("=" * 72)


def proc_name(pid):
    try:
        return psutil.Process(pid).name()
    except Exception:
        return "?"


def proc_exe(pid):
    try:
        return psutil.Process(pid).exe()
    except Exception:
        return "?"


def get_aumid(pid):
    """拿应用的 AUMID（App User Model ID），UWP 应用的唯一标识"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return f"<OpenProcess err {ctypes.get_last_error()}>"
        try:
            fn = k32.GetApplicationUserModelId
        except AttributeError:
            k32.CloseHandle(h)
            return "<API 不存在>"
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        fn.restype = ctypes.c_long
        length = wintypes.UINT(0)
        fn(h, ctypes.byref(length), None)          # 先取所需长度
        if length.value == 0:
            k32.CloseHandle(h)
            return "<空：非 UWP 应用>"
        buf = ctypes.create_unicode_buffer(length.value + 1)
        cap = wintypes.UINT(length.value + 1)
        r = fn(h, ctypes.byref(cap), buf)
        k32.CloseHandle(h)
        return buf.value if r == 0 else f"<err {r}>"
    except Exception as e:
        return f"<{e}>"


def child_windows(hwnd):
    """枚举子窗口，UWP 的真实应用进程通常藏在子窗口里"""
    out = []

    def cb(h, _):
        try:
            _t, pid = win32process.GetWindowThreadProcessId(h)
            out.append((h, pid))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return out


def snapshot_windows():
    """全量窗口快照"""
    rows = []

    def cb(h, _):
        try:
            _t, pid = win32process.GetWindowThreadProcessId(h)
            title = win32gui.GetWindowText(h)
            cls = win32gui.GetClassName(h)
            l, t, r, b = win32gui.GetWindowRect(h)
            rows.append({
                "hwnd": h,
                "pid": pid,
                "proc": proc_name(pid),
                "title": title,
                "class": cls,
                "size": (r - l, b - t),
                "pos": (l, t),
                "visible": bool(win32gui.IsWindowVisible(h)),
                "iconic": bool(win32gui.IsIconic(h)),
            })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return rows


def main():
    original_hwnd = win32gui.GetForegroundWindow()
    print("启动前前台窗口:", original_hwnd, proc_name(
        win32process.GetWindowThreadProcessId(original_hwnd)[1]
    ) if original_hwnd else "")

    before_hwnds = set(r["hwnd"] for r in snapshot_windows())

    hr("1. 启动 UWP 应用")
    for name, uri in UWP_APPS:
        try:
            os.startfile(uri)
            print(f"  已启动: {name} ({uri})")
        except Exception as e:
            print(f"  启动失败: {name} -> {e}")
        time.sleep(1.2)
    print("  等待 3 秒让窗口就绪...")
    time.sleep(3)

    rows = snapshot_windows()

    hr("2. ApplicationFrameHost 窗口分析（UWP 核心问题）")
    afh = [r for r in rows if r["proc"].lower() == "applicationframehost.exe"]
    print(f"  找到 ApplicationFrameHost 窗口: {len(afh)} 个\n")
    for i, w in enumerate(afh):
        print(f"  --- AFH 窗口 #{i} hwnd={w['hwnd']} ---")
        print(f"    标题        : {w['title']!r}")
        print(f"    类名        : {w['class']}")
        print(f"    可见/最小化 : {w['visible']} / {w['iconic']}")
        print(f"    自身 AUMID  : {get_aumid(w['pid'])}")
        kids = child_windows(w["hwnd"])
        print(f"    子窗口数    : {len(kids)}")
        seen = set()
        for kh, kpid in kids:
            if kpid == w["pid"] or kpid in seen:
                continue
            seen.add(kpid)
            print(f"      └ 子窗口 hwnd={kh:<10} pid={kpid:<8} "
                  f"进程={proc_name(kpid):<28} exe={proc_exe(kpid)}")
            print(f"         AUMID : {get_aumid(kpid)}")
        if not seen:
            print("      (子窗口中未发现其它进程)")

    hr("3. 对照：UWP 应用自身的进程有没有独立窗口")
    targets = ["CalculatorApp", "SystemSettings", "Photos", "WinStore.App",
               "Clock", "Microsoft.Photos", "ApplicationFrameHost"]
    for r in rows:
        if any(r["proc"].lower().startswith(t.lower()) for t in targets):
            print(f"  {r['proc']:<30} pid={r['pid']:<8} 可见={r['visible']} "
                  f"标题={r['title'][:40]!r}")

    hr("4. 噪音窗口特征采集")
    print(f"  {'进程':<28} {'类名':<34} {'尺寸':<14} {'位置':<14} 标题")
    print("  " + "-" * 110)
    for r in rows:
        if any(r["proc"].lower().startswith(n.lower()) for n in NOISE_HINTS):
            print(f"  {r['proc'][:27]:<28} {r['class'][:33]:<34} "
                  f"{str(r['size']):<14} {str(r['pos']):<14} {r['title'][:36]!r}")

    hr("5. 全量可见窗口（供交叉核对）")
    vis = [r for r in rows if r["visible"] and r["title"].strip()]
    print(f"  可见且有标题: {len(vis)}")
    for r in vis:
        print(f"  [{r['pid']:<8}] {r['proc'][:26]:<27} hwnd={r['hwnd']:<10} {r['title'][:44]!r}")

    hr("6. 清理：只关闭本次新打开的窗口")
    # 安全原则：① 只关本次新增的窗口 ② 只发 WM_CLOSE ③ 绝不 taskkill
    # ApplicationFrameHost.exe 是共享宿主进程，杀掉会连带关闭其它 UWP 应用
    new_rows = [r for r in snapshot_windows() if r["hwnd"] not in before_hwnds]
    closed = 0
    for w in new_rows:
        try:
            win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
            closed += 1
        except Exception:
            pass
    print(f"  新增窗口 {len(new_rows)} 个，已发送关闭消息 {closed} 个")
    time.sleep(2)
    left = [r for r in snapshot_windows()
            if r["hwnd"] not in before_hwnds and r["visible"] and r["title"].strip()]
    print(f"  仍未关闭的（属正常，UWP 可能需手动关）: {len(left)}")
    for w in left:
        print(f"    hwnd={w['hwnd']} {w['proc']} 标题={w['title'][:40]!r}")

    try:
        if original_hwnd:
            win32gui.SetForegroundWindow(original_hwnd)
            print(f"  已恢复原前台窗口: {original_hwnd}")
    except Exception as e:
        print(f"  恢复焦点失败（可忽略）: {e}")

    hr("采集完成")


if __name__ == "__main__":
    main()
