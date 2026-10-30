# -*- coding: utf-8 -*-
"""仪表盘数据服务：把 SQLite 派生成前端需要的 payload。

Web 服务（web.py）与静态导出脚本（probes/export_dashboard.py）共用同一套口径，
保证 demo 与正式版图形一致。
"""
import datetime
import json

from . import cli, config
from .classifier import ActivityService, aggregate

WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def human(ms):
    s = ms / 1000
    if s < 1:
        return "<1秒"
    if s < 60:
        return f"{s:.0f}秒"
    m = s / 60
    if m < 60:
        return f"{m:.1f}分"
    return f"{m/60:.2f}时"


def day_payload(storage, date):
    """单日 payload（与静态 demo 的数据结构完全一致）。"""
    svc = ActivityService(storage)
    from_ts, to_ts = cli._local_day_to_ts(date)
    day_start = int(datetime.datetime.strptime(date, "%Y-%m-%d").timestamp() * 1000)

    segs = svc.derive(from_ts, to_ts)
    by_app, by_cat, _by_content = aggregate(segs)
    total = sum(by_app.values())
    edited = sum(1 for s in segs if s.get("edited"))

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
            "st": s["start"], "en": s["end"],
            "app": s["app_name"], "ct": s.get("content_title") or "",
            "cat": s.get("category", ""), "dur": human(s["duration_ms"]),
            "prod": s.get("productivity", "neutral"),
            "ed": bool(s.get("edited")),
            "title": s.get("title") or "",
            "s": datetime.datetime.fromtimestamp(s["start"] / 1000).strftime("%H:%M"),
            "e": datetime.datetime.fromtimestamp(s["end"] / 1000).strftime("%H:%M"),
        })
        if (s.get("content_title")
                and s["duration_ms"] >= config.MIN_CONTENT_DISPLAY_MS):
            contents.append({
                "t": datetime.datetime.fromtimestamp(s["start"] / 1000).strftime("%H:%M:%S"),
                "d": human(s["duration_ms"]),
                "title": s["content_title"],
                "app": s["app_name"],
            })

    return {
        "total": human(total), "total_ms": total,
        "by_app": sorted(by_app.items(), key=lambda x: -x[1])[:12],
        "by_cat": sorted(by_cat.items(), key=lambda x: -x[1]),
        "prod": prod, "hourly": hourly,
        "timeline": timeline, "content": contents,
        "edited": edited,
        "cat_options": config.CATEGORY_OPTIONS,
    }


def _dates(start_date, days=7):
    start = datetime.datetime.strptime(start_date, "%Y-%m-%d")
    return [(start + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days)]


def week_context(storage, date):
    """date 所在周（周一起）的周维度数据，给底部两张周图用。"""
    d = datetime.datetime.strptime(date, "%Y-%m-%d").date()
    monday = (d - datetime.timedelta(days=d.weekday())).strftime("%Y-%m-%d")
    dates = _dates(monday, 7)
    labels, weekend, hourly, totals = [], [], [], []
    for dt in dates:
        wd = datetime.datetime.strptime(dt, "%Y-%m-%d").weekday()
        labels.append(WD[wd])
        weekend.append(wd >= 5)
        p = day_payload(storage, dt)
        hourly.append(p["hourly"])
        totals.append(p["total_ms"])
    return {
        "week_start": monday,
        "week_dates": [x[5:] for x in dates],
        "week_labels": labels,
        "weekend": weekend,
        "week_hourly": hourly,
        "daily_totals_ms": totals,
    }


def week_payload(storage, start_date):
    """周汇总：合成一个 __week__ 日 payload，复用前端的日视图图表。"""
    dates = _dates(start_date, 7)
    ctx = week_context(storage, dates[0])
    agg_app, agg_cat = {}, {}
    prod = {"productive": 0, "neutral": 0, "distracting": 0}
    hourly = [0] * 24
    total = 0
    n_content = 0
    for dt in dates:
        p = day_payload(storage, dt)
        total += p["total_ms"]
        for k, v in p["by_app"]:
            agg_app[k] = agg_app.get(k, 0) + v
        for k, v in p["by_cat"]:
            agg_cat[k] = agg_cat.get(k, 0) + v
        for k in prod:
            prod[k] += p["prod"].get(k, 0)
        for i, v in enumerate(p["hourly"]):
            hourly[i] += v
        n_content += len(p["content"])

    payload = {
        "total": human(total), "total_ms": total,
        "by_app": sorted(agg_app.items(), key=lambda x: -x[1])[:12],
        "by_cat": sorted(agg_cat.items(), key=lambda x: -x[1]),
        "prod": prod, "hourly": hourly,
        "timeline": [], "content": [],
        "content_count": n_content,
    }
    return {"data": {"__week__": payload}, "days": ["__week__"], **ctx}


def app_detail(storage, date, app_name):
    """应用下钻：某天某应用的全部活动段。"""
    svc = ActivityService(storage)
    from_ts, to_ts = cli._local_day_to_ts(date)
    segs = svc.derive(from_ts, to_ts)
    items = [s for s in segs if s["app_name"] == app_name]
    # <1 秒的碎片在明细里没有信息量（多是同标题重复事件），隐藏并计数
    shown = [s for s in items if s["duration_ms"] >= 1000]
    return {
        "date": date, "app": app_name,
        "total_ms": sum(s["duration_ms"] for s in items),
        "hidden": len(items) - len(shown),
        "items": [{
            "s": datetime.datetime.fromtimestamp(s["start"] / 1000).strftime("%H:%M"),
            "e": datetime.datetime.fromtimestamp(s["end"] / 1000).strftime("%H:%M"),
            "st": s["start"], "en": s["end"], "ed": bool(s.get("edited")),
            "cat": s["category"], "d": human(s["duration_ms"]),
            "title": s.get("content_title") or s.get("title") or "",
        } for s in shown],
    }


# ---------- 用户编辑（改分类 / 删段）----------
def set_category(storage, app, start, category, scope="segment"):
    """把某段（scope=segment）或某应用全部（scope=app）改到指定分类。"""
    prod = config.CATEGORY_PRODUCTIVITY.get(category, "neutral")
    payload = {"category": category, "productivity": prod}
    if scope == "app":
        return storage.add_edit("app", app, "set_category", payload)
    payload["start"] = int(start or 0)
    return storage.add_edit("segment", app, "set_category", payload)


def exclude_segment(storage, app, start, end):
    """删除一段活动（不改原始事件，只在派生层排除该区间）。"""
    start, end = int(start or 0), int(end or 0)
    if not start or end <= start:
        raise ValueError("invalid range")
    return storage.add_edit("segment", app, "exclude",
                            {"from": start, "to": end})


def list_edits(storage, date=None):
    """列出编辑记录；给定 date 时只返回与该日相关的（应用级编辑始终相关）。"""
    lo = hi = None
    if date:
        lo, hi = cli._local_day_to_ts(date)
    out = []
    for eid, ts, ttype, tid, action, payload, _ap in storage.list_edits():
        try:
            p = json.loads(payload or "{}")
        except (ValueError, TypeError):
            p = {}
        if lo is not None and ttype != "app":
            a = int(p.get("start") or p.get("from") or 0)
            if not (lo <= a < hi):
                continue
        out.append({"id": eid, "ts": ts, "type": ttype, "target": tid,
                    "action": action, "payload": payload, "p": p})
    return out


def available_days(storage, limit=90):
    """有数据的日期（从最近往前）。"""
    lo, hi = storage.ts_range()
    if not lo:
        return []
    d0 = datetime.datetime.fromtimestamp(lo / 1000).date()
    d1 = datetime.datetime.fromtimestamp(hi / 1000).date()
    out = []
    d = d1
    while d >= d0 and len(out) < limit:
        ds = d.strftime("%Y-%m-%d")
        f, t = cli._local_day_to_ts(ds)
        if storage.iter_events_between(f, t, limit=1):
            out.append(ds)
        d -= datetime.timedelta(days=1)
    return out
