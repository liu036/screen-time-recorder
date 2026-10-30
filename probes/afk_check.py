# -*- coding: utf-8 -*-
"""量化"长时间无事件（休眠/离开）被算入前一个应用"的偏差。"""
import bisect
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.storage import Storage
from screen_time_recorder.classifier import ActivityService
from screen_time_recorder import cli

GAP_CUT_MS = 120_000   # 心跳缺失超过 2 分钟视为断档
TAIL_MS = 60_000       # 断档后最多保留最后一条心跳 + 1 分钟


def main():
    st = Storage()
    svc = ActivityService(st)
    days = sorted({
        datetime.datetime.fromtimestamp(r[0] / 1000).strftime("%Y-%m-%d")
        for r in st.iter_events(0, 10 ** 7)
    })
    for day in days:
        from_ts, to_ts = cli._local_day_to_ts(day)
        events = list(st.iter_events_between(from_ts, to_ts))
        hb_ts = [e[0] for e in events if e[1] == "heartbeat"]
        segs = svc.derive(from_ts, to_ts)
        before = sum(s["duration_ms"] for s in segs)
        after = 0
        cuts = []
        for s in segs:
            start, end = s["start"], s["end"]
            if end - start > GAP_CUT_MS:
                idx = bisect.bisect_right(hb_ts, end) - 1
                if idx >= 0 and hb_ts[idx] >= start and end - hb_ts[idx] > GAP_CUT_MS:
                    cuts.append((end - hb_ts[idx], start, end, hb_ts[idx]))
                    end = hb_ts[idx] + TAIL_MS
                elif idx < 0 or hb_ts[idx] < start:
                    # 段内没有心跳
                    if end - start > GAP_CUT_MS:
                        cuts.append((end - start, start, end, None))
                        end = start + TAIL_MS
            after += max(0, end - start)
        print(f"{day}: before={before/3600000:.2f}h after={after/3600000:.2f}h "
              f"cut={ (before-after)/3600000:.2f}h segments_cut={len(cuts)}")
        for gap, a, b, hb in sorted(cuts, reverse=True)[:5]:
            fa = datetime.datetime.fromtimestamp(a / 1000).strftime("%H:%M")
            fb = datetime.datetime.fromtimestamp(b / 1000).strftime("%H:%M")
            fh = (datetime.datetime.fromtimestamp(hb / 1000).strftime("%H:%M")
                  if hb else "无心跳")
            print(f"   {gap/60000:.0f}分空白  segment {fa}->{fb}  最后心跳 {fh}")
    st.close()


if __name__ == "__main__":
    main()
