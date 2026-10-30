# -*- coding: utf-8 -*-
"""导出仪表盘 demo 数据，并注入到 dashboard_demo.html 模板生成成品。"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.storage import Storage
from screen_time_recorder.classifier import ActivityService
from screen_time_recorder import cli, config

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(BASE, "dashboard_demo.html")
OUT_HTML = os.path.join(BASE, "仪表盘demo.html")


def human(ms):
    s = ms / 1000
    if s < 60:
        return f"{s:.0f}秒"
    m = s / 60
    if m < 60:
        return f"{m:.1f}分"
    return f"{m/60:.2f}时"


def main():
    st = Storage()
    svc = ActivityService(st)
    days = sorted({
        datetime.datetime.fromtimestamp(r[0] / 1000).strftime("%Y-%m-%d")
        for r in st.iter_events(0, 10 ** 7)
    })
    out = {"days": days, "data": {}}
    for day in days:
        from_ts, to_ts = cli._local_day_to_ts(day)
        day_start = int(datetime.datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)
        segs = svc.derive(from_ts, to_ts)
        by_app, by_cat, by_content = svc.durations(from_ts, to_ts)
        total = sum(by_app.values())
        hourly = [0] * 24
        prod = {"productive": 0, "neutral": 0, "distracting": 0}
        timeline = []
        contents = []
        for s in segs:
            h = datetime.datetime.fromtimestamp(s["start"] / 1000).hour
            hourly[h] += s["duration_ms"]
            p = s.get("productivity", "neutral")
            prod[p] = prod.get(p, 0) + s["duration_ms"]
            sf = max(0.0, min(1.0, (s["start"] - day_start) / 86400000))
            ef = max(sf, min(1.0, (s["end"] - day_start) / 86400000))
            timeline.append({
                "sf": round(sf, 5), "ef": round(ef, 5),
                "app": s["app_name"], "ct": s.get("content_title") or "",
                "cat": s.get("category", ""), "dur": human(s["duration_ms"]),
                "s": datetime.datetime.fromtimestamp(s["start"] / 1000).strftime("%H:%M"),
                "e": datetime.datetime.fromtimestamp(s["end"] / 1000).strftime("%H:%M"),
            })
            if (s.get("content_title")
                    and s["duration_ms"] >= config.MIN_CONTENT_DISPLAY_MS):
                contents.append({
                    "t": datetime.datetime.fromtimestamp(
                        s["start"] / 1000).strftime("%H:%M:%S"),
                    "d": human(s["duration_ms"]),
                    "title": s["content_title"],
                    "app": s["app_name"],
                })
        out["data"][day] = {
            "total": human(total), "total_ms": total,
            "by_app": sorted(by_app.items(), key=lambda x: -x[1])[:12],
            "by_cat": sorted(by_cat.items(), key=lambda x: -x[1]),
            "prod": prod,
            "hourly": hourly,
            "timeline": timeline,
            "content": contents,
        }
    st.close()

    # 一周维度（punch card / hairline line 用）
    WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    out["week_labels"] = []
    out["week_dates"] = []
    out["weekend"] = []
    for day in days:
        wd = datetime.datetime.strptime(day, "%Y-%m-%d").weekday()
        out["week_labels"].append(WD[wd])
        out["week_dates"].append(day[5:])          # MM-DD
        out["weekend"].append(wd >= 5)
    out["week_hourly"] = [out["data"][d]["hourly"] for d in days]
    out["daily_totals_ms"] = [out["data"][d]["total_ms"] for d in days]

    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    if "/*__DATA__*/null" not in html:
        raise SystemExit("模板缺少数据占位符 /*__DATA__*/null")
    html = html.replace("/*__DATA__*/null", json.dumps(out, ensure_ascii=False))
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print("written:", OUT_HTML)
    for day in days:
        d = out["data"][day]
        print(day, d["total"], "| timeline", len(d["timeline"]),
              "| content", len(d["content"]))


if __name__ == "__main__":
    main()
