# -*- coding: utf-8 -*-
"""
T3 + T6 + T8 三测合一采集探针
================================
一次会话同时采集四类数据，回答 Q3 / Q5 / Q6 / Q10 / F10 / F3：

  fg_event  : SetWinEventHook 事件驱动的前台切换（事件流）
  fg_poll   : 250ms 轮询检测到的前台变化（对拍基线）
  fg_beat   : 60s 心跳（保活 + 空闲/idle 检测）
  snap      : 窗口布局快照（1s 检测，布局变化时才写入）
  res       : 5s 一次进程资源采样（CPU / 内存）
  lock      : 锁屏 / 无前台窗口事件

用法:
  python probe_session.py --duration 25 [--outdir ...]
"""

import argparse
import ctypes
import io
import json
import os
import sys
import time
import threading
import psutil
from ctypes import wintypes
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import win32api
import win32con
import win32gui
import win32process

# ---------------------------------------------------------------- 基础工具

DWMWA_CLOAKED = 14
_dwmapi = ctypes.windll.dwmapi


def is_cloaked(hwnd):
    """窗口是否被虚拟桌面/遮罩隐藏（Win10+）"""
    try:
        val = wintypes.BOOL()
        hr = _dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_CLOAKED),
            ctypes.byref(val),
            ctypes.sizeof(val),
        )
        if hr == 0:
            return bool(val.value)
    except Exception:
        pass
    return False


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]


def get_idle_ms():
    """系统级空闲时长（毫秒）"""
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        return win32api.GetTickCount() - lii.dwTime
    except Exception:
        return -1


_proc_cache = {}


def proc_info(pid):
    """进程名 / exe 路径（带缓存，key 含 create_time 防 pid 复用）"""
    try:
        p = psutil.Process(pid)
        key = (pid, round(p.create_time(), 3))
        if key not in _proc_cache:
            try:
                exe = p.exe()
            except Exception:
                exe = ""
            _proc_cache[key] = (p.name(), exe)
        return _proc_cache[key]
    except Exception:
        return ("?", "")


def resolve_uwp(hwnd, pid, proc):
    """UWP 解析：AFH 宿主 → 子窗口 pid → 真实应用进程名"""
    if proc.lower() != "applicationframehost.exe":
        return None
    result = None

    def cb(h, _):
        nonlocal result
        if result:
            return True
        try:
            _, cpid = win32process.GetWindowThreadProcessId(h)
            if cpid and cpid != pid:
                name, _ = proc_info(cpid)
                if name and name.lower() != "applicationframehost.exe":
                    result = (cpid, name)
                    return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return result


def winfo(hwnd, deep=False):
    """采集单个窗口的完整信息"""
    try:
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        title = ""
    try:
        iconic = bool(win32gui.IsIconic(hwnd))
    except Exception:
        iconic = False
    try:
        cls = win32gui.GetClassName(hwnd)
    except Exception:
        cls = ""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        pid = 0
    proc, exe = proc_info(pid)
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        rect = [l, t, r - l, b - t]
    except Exception:
        rect = [0, 0, 0, 0]

    d = {
        "hwnd": hwnd,
        "pid": pid,
        "proc": proc,
        "exe": exe,
        "cls": cls,
        "title": title,
        "rect": rect,
        "visible": bool(win32gui.IsWindowVisible(hwnd)),
        "iconic": iconic,
        "cloaked": is_cloaked(hwnd),
    }
    if deep:
        uwp = resolve_uwp(hwnd, pid, proc)
        if uwp:
            d["uwp_pid"], d["uwp_proc"] = uwp
    return d


def effective_visible(w):
    """真正算'看得见'的窗口：可见 + 非最小化 + 未 cloaked + 有实际面积"""
    return w["visible"] and not w["iconic"] and not w["cloaked"] \
        and w["rect"][2] > 0 and w["rect"][3] > 0


def cover_ratio(rect, monitor_rects):
    """窗口矩形落在显示器内的面积占比（处理 Win11 的 8px 阴影外扩）"""
    l, t, w, h = rect
    r, b = l + w, t + h
    total = float(w * h) if w > 0 and h > 0 else 1.0
    best = 0.0
    for ml, mt, mr, mb in monitor_rects:
        il, it = max(l, ml), max(t, mt)
        ir, ib = min(r, mr), min(b, mb)
        if ir > il and ib > it:
            best = max(best, (ir - il) * (ib - it) / total)
    return round(best, 3)


# ---------------------------------------------------------------- 采集器

# ---- Win32 事件钩子（pywin32 312 的 win32gui 无 SetWinEventHook，改用 ctypes 直调）----
user32 = ctypes.windll.user32
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_NAMECHANGE = 0x800C
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
PM_REMOVE = 0x0001

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,    # hWinEventHook
    wintypes.DWORD,     # event
    wintypes.HWND,      # hwnd
    wintypes.LONG,      # idObject
    wintypes.LONG,      # idChild
    wintypes.DWORD,     # idEventThread
    wintypes.DWORD,     # dwmsEventTime
)


class Recorder:
    def __init__(self, outdir):
        self.outdir = outdir
        os.makedirs(outdir, exist_ok=True)
        self.path = os.path.join(outdir, "session.jsonl")
        self.f = open(self.path, "w", encoding="utf-8")
        self.lock = threading.Lock()
        self.counts = {}
        self.t0 = time.time()

    def write(self, type_, data=None):
        rec = {
            "t": int(time.time() * 1000),
            "t_iso": datetime.now().isoformat(timespec="milliseconds"),
            "el": round(time.time() - self.t0, 3),
            "type": type_,
        }
        if data:
            rec.update(data)
        with self.lock:
            self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.f.flush()
            self.counts[type_] = self.counts.get(type_, 0) + 1

    def close(self):
        with self.lock:
            self.f.close()


class Session:
    def __init__(self, rec, duration_s):
        self.rec = rec
        self.duration = duration_s
        self.stop_evt = threading.Event()
        self.proc = psutil.Process(os.getpid())
        self.proc.cpu_percent(None)  # prime

        self.last_fg_hwnd = None
        self.last_fg_title = None
        self.last_layout_sig = None
        self.last_beat = 0.0
        self.last_res = 0.0
        self.last_snap = 0.0
        self.was_locked = False

        self.monitors = self.detect_monitors()
        self.monitor_rects = [m["rect"] for m in self.monitors] or \
            [[0, 0, win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)]]

    @staticmethod
    def detect_monitors():
        try:
            mons = []
            for h, _, r in win32api.EnumDisplayMonitors():
                mons.append({"handle": int(h), "rect": list(r)})
            return mons
        except Exception:
            return []

    # ---- 事件驱动：SetWinEventHook（ctypes 直调 + 自建消息循环）----
    def hook_thread(self):
        def on_fg(hook, event, hwnd, idObject, idChild, thread, ts):
            try:
                if not hwnd:
                    self.rec.write("fg_event", {"hwnd": 0, "note": "null_hwnd",
                                                "event": event})
                    return
                info = winfo(hwnd, deep=True)
                info["idle_ms"] = get_idle_ms()
                info["event"] = event
                self.rec.write("fg_event", info)
            except Exception as e:
                self.rec.write("err", {"where": "hook", "msg": str(e)})

        self._cb_ref = WINEVENTPROC(on_fg)  # 必须保持引用，否则被 GC
        try:
            hook = user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND,
                EVENT_SYSTEM_FOREGROUND,
                0,
                self._cb_ref,
                0,
                0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
            )
        except Exception as e:
            self.rec.write("meta_hook", {"status": "fail", "msg": str(e)})
            return

        if not hook:
            self.rec.write("meta_hook", {"status": "fail",
                                         "msg": "SetWinEventHook returned NULL"})
            return
        self.rec.write("meta_hook", {"status": "ok",
                                     "event": "EVENT_SYSTEM_FOREGROUND",
                                     "hook": int(hook)})

        msg = wintypes.MSG()
        while not self.stop_evt.is_set():
            try:
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    self.stop_evt.wait(0.02)
            except Exception:
                self.stop_evt.wait(0.05)
        try:
            user32.UnhookWinEvent(wintypes.HANDLE(hook))
        except Exception:
            pass

    # ---- 定时采集主循环 ----
    def poll_thread(self):
        while not self.stop_evt.is_set():
            now = time.time()
            try:
                hwnd = win32gui.GetForegroundWindow()
            except Exception:
                hwnd = 0

            # 锁屏 / 无前台窗口
            locked = (hwnd == 0)
            if locked != self.was_locked:
                self.rec.write("lock", {"locked": locked, "hwnd": hwnd})
                self.was_locked = locked

            if hwnd:
                try:
                    title = win32gui.GetWindowText(hwnd)
                except Exception:
                    title = ""
                if hwnd != self.last_fg_hwnd or title != self.last_fg_title:
                    info = winfo(hwnd, deep=True)
                    info["idle_ms"] = get_idle_ms()
                    self.rec.write("fg_poll", info)
                    self.last_fg_hwnd = hwnd
                    self.last_fg_title = title
            else:
                self.last_fg_hwnd = None
                self.last_fg_title = None

            # 布局快照（1s）
            if now - self.last_snap >= 1.0:
                self.last_snap = now
                self.detect_layout_change()

            # 资源采样（5s）
            if now - self.last_res >= 5.0:
                self.last_res = now
                try:
                    self.rec.write("res", {
                        "cpu": self.proc.cpu_percent(None),
                        "rss_mb": round(self.proc.memory_info().rss / 1048576, 2),
                        "threads": self.proc.num_threads(),
                    })
                except Exception:
                    pass

            # 心跳（60s）
            if now - self.last_beat >= 60.0:
                self.last_beat = now
                self.rec.write("fg_beat", {
                    "hwnd": hwnd,
                    "idle_ms": get_idle_ms(),
                    "monitors": len(self.monitors),
                })

            self.stop_evt.wait(0.25)

    def enum_all_windows(self):
        """枚举窗口全量（含最小化，用字段标记，由分析阶段决定过滤策略）"""
        out = []

        def cb(h, _):
            try:
                if not win32gui.IsWindowVisible(h):
                    return True
                if is_cloaked(h):
                    return True
                t = win32gui.GetWindowText(h)
                if not t.strip():
                    return True
                w = winfo(h)
                w["cover"] = cover_ratio(w["rect"], self.monitor_rects)
                out.append(w)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(cb, None)
        return out

    def detect_layout_change(self):
        """1s 检测窗口布局是否变化，变化才写入完整快照"""
        try:
            wins = self.enum_all_windows()
            vis = [w for w in wins if effective_visible(w)]
            sig = tuple(sorted(
                (w["hwnd"], tuple(w["rect"]), w["cover"]) for w in vis
            ))
            if sig != self.last_layout_sig:
                self.last_layout_sig = sig
                self.rec.write("snap", {
                    "visible_count": len(vis),
                    "total_count": len(wins),
                    "iconic_count": sum(1 for w in wins if w["iconic"]),
                    "visible_hwnds": [w["hwnd"] for w in vis],
                    "windows": wins,
                })
        except Exception as e:
            self.rec.write("err", {"where": "snap", "msg": str(e)})

    def run(self):
        self.rec.write("meta_start", {
            "duration_s": self.duration,
            "monitors": self.monitors,
            "screen": [win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)],
            "py": sys.version.split()[0],
        })
        t1 = threading.Thread(target=self.hook_thread, daemon=True)
        t2 = threading.Thread(target=self.poll_thread, daemon=True)
        t1.start()
        t2.start()

        end = time.time() + self.duration
        try:
            while time.time() < end:
                left = int(end - time.time())
                if left % 60 == 0:
                    print(f"  ...剩余 {left//60} 分 {left%60:02d} 秒", flush=True)
                time.sleep(1)
        except KeyboardInterrupt:
            print("  收到中断，提前结束")
        self.stop_evt.set()
        t1.join(timeout=3)
        t2.join(timeout=3)
        self.rec.write("meta_end", {})
        self.rec.close()


def summarize(rec_path):
    import statistics
    counts = {}
    cpus, rsss = [], []
    fg_polls = 0
    with open(rec_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            counts[r["type"]] = counts.get(r["type"], 0) + 1
            if r["type"] == "res":
                cpus.append(r.get("cpu", 0))
                rsss.append(r.get("rss_mb", 0))
            if r["type"] == "fg_poll":
                fg_polls += 1
    print()
    print("=== 采集摘要 ===")
    for k in sorted(counts):
        print(f"  {k:<12} {counts[k]}")
    if cpus:
        print(f"  CPU%  平均 {statistics.mean(cpus):.2f}  最大 {max(cpus):.2f}")
        print(f"  内存MB 平均 {statistics.mean(rsss):.1f}  最大 {max(rsss):.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=25.0, help="采集时长（分钟）")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = args.outdir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "sessions", stamp
    )
    rec = Recorder(outdir)
    print(f"采集开始：{stamp}")
    print(f"时长：{args.duration} 分钟")
    print(f"输出：{rec.path}")
    print("（正常用电脑即可，脚本后台静默运行）", flush=True)

    s = Session(rec, args.duration * 60)
    s.run()

    print()
    print(f"完成。输出目录：{outdir}")
    summarize(rec.path)


if __name__ == "__main__":
    main()
