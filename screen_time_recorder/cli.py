# -*- coding: utf-8 -*-
"""CLI：查看 / 导出 / 删除（MVP-7）。"""
import sys
import io
import json
import csv
import time
from datetime import datetime

# 原地重配编码（不会像重新包装 stdout 那样让旧对象被 GC 后关闭底层 buffer）
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from .storage import Storage
from .classifier import ActivityService


def _local_day_to_ts(day_str):
    d = datetime.strptime(day_str, "%Y-%m-%d")
    from_ts = int(time.mktime(d.timetuple())) * 1000
    to_ts = from_ts + 86400000
    # 当天：结束时间不能超过"现在"，否则会把尚未发生的时间（午夜前空白）
    # 算给最后一个前台应用，导致今日总时长严重虚高。
    if day_str == datetime.now().strftime("%Y-%m-%d"):
        now_ms = int(time.time() * 1000)
        to_ts = min(to_ts, now_ms)
    return from_ts, to_ts


def _human(ms):
    s = ms / 1000.0
    if s < 1:
        return "<1秒"
    if s < 60:
        return f"{s:.0f}秒"
    m = s / 60.0
    if m < 60:
        return f"{m:.1f}分"
    return f"{m / 60.0:.2f}时"


def cmd_view(st, day_str, exclude_afk=False):
    from_ts, to_ts = _local_day_to_ts(day_str)
    svc = ActivityService(st)
    by_app, by_cat, by_content = svc.durations(from_ts, to_ts, exclude_afk=exclude_afk)
    top = sorted(by_app.items(), key=lambda x: -x[1])[:10]
    # 合计用全量：Top10 只用于展示，否则应用数 >10 时合计偏小、百分比之和会超 100%
    total = sum(by_app.values())
    label = "专注" if exclude_afk else "前台"
    print(f"\n===== {day_str} 应用时长 Top10（{label}口径）=====")
    if not top:
        print("  （无数据）")
        return
    for name, d in top:
        pct = (d / total * 100) if total else 0
        print(f"  {name:<22} {_human(d):>8}  {pct:5.1f}%")
    print(f"  {'—' * 30}\n  合计 {_human(total)}")
    print("===== 类别分布 =====")
    for cat, d in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {cat:<14} {_human(d):>8}")

    # 内容级明细：对记录了 content_title 的应用，按内容聚合时长 Top
    if by_content:
        from collections import defaultdict
        by_app_content = defaultdict(list)
        for (app_name, ct), d in by_content.items():
            by_app_content[app_name].append((ct, d))
        print("===== 内容明细（内容级记录）=====")
        for app_name in sorted(by_app_content,
                               key=lambda a: -sum(d for _, d in by_app_content[a])):
            items = sorted(by_app_content[app_name], key=lambda x: -x[1])
            print(f"  【{app_name}】")
            for ct, d in items[:8]:
                print(f"    {_human(d):>8}  {ct[:60]}")


def cmd_apps(st):
    print("===== 已记录应用 =====")
    for r in st.get_all_apps():
        print(f"  app_id={r[0]} {r[2] or r[1]}  ({r[1]}, {r[3]})")


def cmd_export(st, day_str, fmt="csv", out=None):
    from_ts, to_ts = _local_day_to_ts(day_str)
    svc = ActivityService(st)
    segs = svc.derive(from_ts, to_ts)
    if fmt == "json":
        path = out or f"export_{day_str}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"day": day_str, "activities": segs}, f,
                      ensure_ascii=False, indent=2, default=str)
    else:
        path = out or f"export_{day_str}.csv"
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["start", "end", "duration_ms", "app_name", "category",
                        "productivity", "afk", "title", "content_title"])
            for s in segs:
                w.writerow([s["start"], s["end"], s["duration_ms"], s["app_name"],
                            s["category"], s["productivity"], s["afk"], s["title"],
                            s.get("content_title") or ""])
    print(f"已导出: {path}  （{len(segs)} 段活动）")


def cmd_delete(st, day_str):
    from_ts, to_ts = _local_day_to_ts(day_str)
    n_e, n_s = st.delete_range(from_ts, to_ts)
    print(f"已删除 {day_str}: events {n_e} 条, snapshots {n_s} 条")
