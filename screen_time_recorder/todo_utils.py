# -*- coding: utf-8 -*-
"""待办窗口的轻量工具：HTTP、日志、日期与文本。

统一放在这里，todo_window 与 pomodoro_panel 都能用，避免互相 import。
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from . import config

API_BASE = os.environ.get("STR_API", "http://127.0.0.1:8765")


def api(path, method="GET", body=None, timeout=3.0):
    """调主进程的本地 API；失败抛异常，由调用方决定是否兜底。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def log(msg):
    """pythonw 下 sys.stdout 是 None，print 会抛异常；异常路径单独落一份日志。"""
    try:
        path = os.path.join(os.path.dirname(config.DB_PATH), "todo.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def fmt_when(ts_ms):
    """提醒/截止时间 → 人类可读：逾期 / 今天 18:00 / 明天 09:00 / 10-24 18:00。"""
    if not ts_ms:
        return ""
    dt = datetime.fromtimestamp(ts_ms / 1000.0)
    today = datetime.now().date()
    d = dt.date()
    hm = dt.strftime("%H:%M")
    if d < today:
        days = (today - d).days
        return "逾期 %d 天" % days if days > 1 else "逾期"
    if d == today:
        return "今天 " + hm
    if d == today + timedelta(days=1):
        return "明天 " + hm
    return d.strftime("%m-%d") + " " + hm


def next_priority(current):
    """低(0) → 中(1) → 高(2) → 低(0)。

    注意不能写 `current or 1`：0 会被吞成 1，循环就退化成灰/琥珀两态。
    """
    cur = min(max(int(current if current is not None else 1), 0), 2)
    return (cur + 1) % 3


def ellipsize(text, font, max_px):
    """按像素宽度截断并补省略号（Label 不会自动省略，长标题会撑破布局）。"""
    text = text or ""
    if max_px <= 0 or font.measure(text) <= max_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + "…") if lo else "…"
