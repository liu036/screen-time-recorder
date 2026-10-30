# -*- coding: utf-8 -*-
"""生成每日记录明细（Markdown，含小时时间线与内容级记录）。"""
import datetime
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.storage import Storage
from screen_time_recorder.classifier import ActivityService
from screen_time_recorder import cli, config

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "5-每日记录明细.md")


def fmt(ts, f="%H:%M:%S"):
    return datetime.datetime.fromtimestamp(ts / 1000).strftime(f)


def human(ms):
    s = ms / 1000
    if s < 60:
        return f"{s:.0f}秒"
    m = s / 60
    if m < 60:
        return f"{m:.1f}分"
    return f"{m/60:.2f}时"


def day_report(day, segs):
    lines = []
    total = sum(s["duration_ms"] for s in segs)
    # 内容明细只展示 >=1 分钟的内容（时长仍已计入 total）
    contents = [s for s in segs
                if s.get("content_title")
                and s["duration_ms"] >= config.MIN_CONTENT_DISPLAY_MS]
    lines.append(f"## {day}")
    lines.append("")
    lines.append(f"**概览**：前台 {human(total)} · {len(segs)} 段 · 内容级 {len(contents)} 条")
    lines.append("")

    # 小时时间线
    hours = defaultdict(lambda: {"apps": Counter(), "contents": Counter()})
    for s in segs:
        h = fmt(s["start"], "%H:00")
        hours[h]["apps"][s["app_name"]] += s["duration_ms"]
        if (s.get("content_title")
                and s["duration_ms"] >= config.MIN_CONTENT_DISPLAY_MS):
            hours[h]["contents"][s["content_title"]] += s["duration_ms"]
    lines.append("### 小时时间线")
    lines.append("")
    lines.append("| 时段 | 主要应用（时长） | 内容（时长） |")
    lines.append("|---|---|---|")
    for h in sorted(hours):
        apps = " / ".join(f"{a} {human(d)}" for a, d in hours[h]["apps"].most_common(3))
        conts = " ; ".join(
            f"{c[:38]} {human(d)}" for c, d in hours[h]["contents"].most_common(3))
        lines.append(f"| {h} | {apps or '—'} | {conts or '—'} |")
    lines.append("")

    # 内容级记录
    if contents:
        cons = sorted(contents, key=lambda x: x["start"])
        if len(cons) > 60:
            cons = sorted(
                sorted(contents, key=lambda x: -x["duration_ms"])[:60],
                key=lambda x: x["start"])
        lines.append(f"### 内容级记录（共 {len(contents)} 条，展示 {len(cons)} 条）")
        lines.append("")
        lines.append("| 时间 | 时长 | 内容 |")
        lines.append("|---|---|---|")
        for s in cons:
            lines.append(
                f"| {fmt(s['start'])} | {human(s['duration_ms'])} | "
                f"{s['content_title'][:72]} |")
        lines.append("")

    # 其他应用长段
    long_segs = [s for s in segs
                 if s["duration_ms"] >= 180000 and not s.get("content_title")]
    if long_segs:
        lines.append("### 其他应用（单段 ≥3 分钟）")
        lines.append("")
        lines.append("| 时间 | 时长 | 应用 | 窗口标题 |")
        lines.append("|---|---|---|---|")
        for s in sorted(long_segs, key=lambda x: x["start"])[:50]:
            title = (s.get("title") or "—")[:60]
            lines.append(
                f"| {fmt(s['start'], '%H:%M')}–{fmt(s['end'], '%H:%M')} | "
                f"{human(s['duration_ms'])} | {s['app_name']} | {title} |")
        lines.append("")
    return "\n".join(lines)


def main():
    st = Storage()
    svc = ActivityService(st)
    days = sorted({
        datetime.datetime.fromtimestamp(r[0] / 1000).strftime("%Y-%m-%d")
        for r in st.iter_events(0, 10 ** 7)
    })
    out = [
        "# 每日记录明细",
        "",
        f"> 生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    for day in days:
        segs = svc.derive(*cli._local_day_to_ts(day))
        out.append(day_report(day, segs))
        out.append("---")
        out.append("")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("written:", OUT)
    print("days:", days)
    st.close()


if __name__ == "__main__":
    main()
