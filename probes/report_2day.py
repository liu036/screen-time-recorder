# -*- coding: utf-8 -*-
"""两日常驻测试报告数据采集（只读）。"""
import datetime
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.storage import Storage
from screen_time_recorder.classifier import ActivityService
from screen_time_recorder import cli


def fmt_ts(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def main():
    st = Storage()
    rows = list(st.iter_events(0, 10000000))
    n_e, n_s = len(rows), st.count_snapshots()
    print(f"events={n_e} snapshots={n_s}")
    if not rows:
        print("无数据")
        st.close()
        return
    print(f"时间范围: {fmt_ts(rows[0][0])} ~ {fmt_ts(rows[-1][0])}")
    print(f"跨度: {(rows[-1][0]-rows[0][0])/3600000:.1f} 小时")

    by_day = Counter(
        datetime.datetime.fromtimestamp(r[0] / 1000).strftime("%Y-%m-%d")
        for r in rows)
    print("按天事件数:", dict(sorted(by_day.items())))

    et = Counter(r[1] for r in rows)
    print("事件类型:", dict(et))

    # 心跳断档
    hb = [r[0] for r in rows if r[1] == "heartbeat"]
    gaps = [(hb[i] - hb[i - 1]) / 1000 for i in range(1, len(hb))]
    if gaps:
        print(f"心跳: {len(hb)} 条, 最大间隔 {max(gaps):.0f}s, "
              f">180s 次数 {sum(1 for g in gaps if g > 180)}")
        for g, t in sorted(((gaps[i], hb[i]) for i in range(len(gaps))),
                           reverse=True)[:5]:
            print(f"  断档 {g:.0f}s @ {fmt_ts(t)}")

    # 空白时段（相邻事件 > 5min）
    ts = [r[0] for r in rows]
    blanks = []
    for i in range(1, len(ts)):
        gap = (ts[i] - ts[i - 1]) / 1000
        if gap > 300:
            blanks.append((gap, ts[i - 1], ts[i]))
    print(f"空白段(>5min): {len(blanks)} 段, 合计 {sum(g for g, _, _ in blanks)/60:.1f} 分钟")
    for g, a, b in sorted(blanks, reverse=True)[:8]:
        print(f"  {g/60:.1f}分  {fmt_ts(a)} -> {fmt_ts(b)}")

    # 按天活动统计
    for day in sorted(by_day.keys()):
        from_ts, to_ts = cli._local_day_to_ts(day)
        svc = ActivityService(st)
        by_app, by_cat, by_content = svc.durations(from_ts, to_ts)
        total = sum(by_app.values())
        print(f"\n--- {day} ---")
        print(f"  前台总时长: {total/3600000:.2f} 小时")
        for name, d in sorted(by_app.items(), key=lambda x: -x[1])[:8]:
            print(f"    {name:<24} {d/3600000:.2f}h")
        print("  类别:", {k: round(v / 3600000, 2) for k, v in
                         sorted(by_cat.items(), key=lambda x: -x[1])})
        print(f"  内容级条目: {len(by_content)}")
        for (app, ct), d in sorted(by_content.items(), key=lambda x: -x[1])[:8]:
            print(f"      {d/60000:.1f}分  {ct[:52]}")

    # 噪音检查（10-25 之后）
    print("\n--- 10-25 之后出现的应用（次数）---")
    since = int(datetime.datetime(2026, 10, 25, 0, 0).timestamp() * 1000)
    apps = {r[0]: (r[2] or r[1]) for r in st.get_all_apps()}
    seen = Counter(apps.get(r[3], "?") for r in rows if r[0] >= since)
    for name, c in seen.most_common(15):
        print(f"  {name:<26} {c}")
    st.close()


if __name__ == "__main__":
    main()
