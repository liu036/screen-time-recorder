# -*- coding: utf-8 -*-
"""可见性快照（MVP-4）。

落实 T6 结论：
- 1s 检测窗口布局，签名变化才写（抽屉式，控制数据量）
- 三重可见性判据：!IsIconic + 面积 > 屏幕 1% + 类名非黑名单（cover 指标失效，见报告 §5.3）
- rect clamp：防 Win11 窗口阴影外扩（最大化实测 2576x1408 > 屏幕 2560x1440）导致 cover > 1.0
- MVP 只落盘不展示，为 V1 分屏时间线预留历史数据
"""
import threading
import time

import win32api
import win32con
import win32gui
import win32process
import psutil

from . import config
from .collector import normalize_app_name, resolve_real_pid


class SnapshotCollector:
    def __init__(self, storage):
        self.storage = storage
        self.stop = threading.Event()
        self.last_sig = None

    def _screen(self):
        w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        return w, h

    def _collect(self):
        sw, sh = self._screen()
        screen_area = sw * sh
        wins = []

        def cb(h, _):
            try:
                if not win32gui.IsWindowVisible(h):
                    return True
                if win32gui.IsIconic(h):           # 最小化不算可见（T6 实测）
                    return True
                cls = win32gui.GetClassName(h) or ""
                _, pid = win32process.GetWindowThreadProcessId(h)
                proc = psutil.Process(pid).name()
                # UWP：AFH 是宿主，解析到真实应用（与 collector 保持一致）
                if proc.lower() == config.UWP_HOST_PROC.lower():
                    real_pid = resolve_real_pid(h, pid)
                    proc = psutil.Process(real_pid).name()
                # 噪音过滤（与 collector 一致）
                if proc in config.NOISE_PROC:
                    return True
                if any(k in proc.lower() for k in config.NOISE_PROC_KEYWORDS):
                    return True
                if cls in config.NOISE_CLASS:
                    return True
                if (proc in config.PROC_CLASS_GUARD
                        and cls not in config.PROC_CLASS_GUARD[proc]):
                    return True
                l, t, r, b = win32gui.GetWindowRect(h)
                w = r - l
                hh = b - t
                if w <= 0 or hh <= 0:
                    return True
                if w * hh < screen_area * config.SNAPSHOT_MIN_AREA_RATIO:  # 三重判据
                    return True
                # rect clamp（防阴影外扩）
                cl = max(0, l)
                ct = max(0, t)
                cr = min(sw, r)
                cb = min(sh, b)
                cw = max(0, cr - cl)
                ch = max(0, cb - ct)
                cover = (cw * ch) / screen_area if screen_area else 0.0
                wins.append({
                    "hwnd": int(h), "process_name": proc, "cls": cls,
                    "rect": (cl, ct, cw, ch), "cover": cover,
                })
            except Exception:
                pass
            return True

        win32gui.EnumWindows(cb, None)

        # 布局签名：变化才写（抽屉式）
        sig = tuple(sorted((w["hwnd"], w["rect"], w["cls"]) for w in wins))
        if sig == self.last_sig:
            return
        self.last_sig = sig

        now = int(time.time() * 1000)
        for w in wins:
            app_id = self.storage.ensure_app(
                w["process_name"], None,
                normalize_app_name(w["process_name"]), w["cls"])
            self.storage.buffer_snapshot(
                ts=now, hwnd=w["hwnd"], app_id=app_id,
                rect=",".join(map(str, w["rect"])),
                visibility="visible", cover=round(w["cover"], 4))

    def _loop(self):
        while not self.stop.is_set():
            try:
                self._collect()
            except Exception as e:
                print(f"[snapshot] error: {e}")
            time.sleep(config.SNAPSHOT_INTERVAL_MS / 1000.0)

    def start(self):
        self.stop.clear()  # 支持暂停后恢复
        threading.Thread(target=self._loop, daemon=True).start()
