# -*- coding: utf-8 -*-
"""活动派生 + 规则分类 + 统计（MVP-5 与统计基础）。

落实计划书决策：
- <2s 碎片段丢弃（先过滤噪音再套阈值，见报告 T3）
- AFK 标记（基于 heartbeat 的 idle，段内任一 heartbeat idle>阈值则标 afk）
- 规则分类（category_rules 正则匹配，priority 降序）
- 双口径：专注口径（排除 afk）/ 前台口径（含 afk）
"""
import bisect
import json
import re

from . import config

# 断档判定：心跳缺失超过 2 分钟，说明机器休眠/用户离开/关机，长段不能把空白算给前一个应用
GAP_CUT_MS = 120_000
TAIL_MS = 60_000  # 断档后最多保留"最后一条心跳 + 1 分钟"

# 浏览器窗口标题 → 内容级标题（MVP 白捡的内容级：页面标题就在窗口标题里）
BROWSER_PROCS = ("firefox.exe", "chrome.exe", "msedge.exe", "brave.exe")
BROWSER_SUFFIX_RE = re.compile(
    r"\s+[—–-]\s+(Mozilla Firefox|Google Chrome|Microsoft Edge|Brave).*$")
BROWSER_HOME_TITLES = {
    "mozilla firefox", "google chrome", "microsoft edge", "new tab",
    "新标签页", "起始页",
    "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili",
}


# 浏览器标题里的播放器噪音（不算页面内容）
BROWSER_NOISE_RE = re.compile(
    r"(上次播放至[：:].*$|^视频加载中.*$|^播放中$|^加载中.*$)")


def clean_browser_title(title):
    """从窗口标题中解析页面标题；首页/播放器噪音/空标题返回空串。"""
    t = BROWSER_SUFFIX_RE.sub("", (title or "").strip()).strip()
    t = BROWSER_NOISE_RE.sub("", t).strip()
    if not t or t.lower() in BROWSER_HOME_TITLES:
        return ""
    return t


def classify_with(app, rules, title=""):
    """用已加载的 app 元组与规则匹配（批量场景避免逐段查库）。

    app 需为 5 元组 (app_id, process_name, exe_path, app_name, cls)。
    """
    process_name = app[1] if app else ""
    app_name = app[3] if app else ""
    for _rid, match_field, pattern, category, sub_category, productivity, _priority in rules:
        if match_field == "process_name":
            target = process_name
        elif match_field == "title":
            target = title or ""
        elif match_field == "app_name":
            target = app_name
        else:
            target = ""
        try:
            if re.search(pattern, target or "", re.IGNORECASE):
                return (category or "未分类", sub_category or "", productivity or "neutral")
        except re.error:
            continue
    return ("未分类", "", "neutral")


def classify(storage, app_id, title=""):
    """兼容入口：按需查库。批量场景请用 classify_with + 预加载。"""
    return classify_with(storage.get_app(app_id), storage.get_rules(), title)


# ---------- 用户编辑层（edits）----------
def load_edits(storage):
    """解析 edits 表 → (应用级分类覆盖, 单段分类覆盖, 排除区间)。

    - set_category + app     : {app_name: (cat, sub, prod)}
    - set_category + segment : {(app_name, start_ms): (cat, sub, prod)}
    - exclude                : [(app_name|None, from_ms, to_ms)]
    """
    app_cat, seg_cat, excludes = {}, {}, []
    try:
        rows = storage.list_edits()
    except Exception:
        return app_cat, seg_cat, excludes
    for _id, _ts, ttype, tid, action, payload, _ap in rows:
        try:
            p = json.loads(payload or "{}")
        except (ValueError, TypeError):
            continue
        tid = tid or ""
        if action == "set_category":
            val = (p.get("category") or "未分类",
                   p.get("sub_category") or "",
                   p.get("productivity") or "neutral")
            if ttype == "app":
                app_cat[tid] = val
            elif ttype == "segment":
                seg_cat[(tid, int(p.get("start") or 0))] = val
        elif action == "exclude":
            f, t = int(p.get("from") or 0), int(p.get("to") or 0)
            if f and t > f:
                excludes.append((tid or None, f, t))
    return app_cat, seg_cat, excludes


def clip_segment(start, end, excludes, app_name):
    """把 [start,end) 减去排除区间，返回剩余片段列表（可能为空、可能是多段）。"""
    pieces = [(start, end)]
    for app, f, t in excludes:
        if app and app != app_name:
            continue
        nxt = []
        for a, b in pieces:
            if t <= a or f >= b:
                nxt.append((a, b))
                continue
            if f > a:
                nxt.append((a, min(f, b)))
            if t < b:
                nxt.append((max(t, a), b))
        pieces = [(a, b) for a, b in nxt if b - a > 0]
        if not pieces:
            break
    return pieces


def aggregate(segs, exclude_afk=False):
    """把活动段聚合成 (by_app, by_cat, by_content)。

    内容明细只展示 >=1 分钟的内容；更短的内容时长仍计入 by_app/by_cat。
    """
    by_app, by_cat, by_content = {}, {}, {}
    for s in segs:
        if exclude_afk and s["afk"]:
            continue
        d = s["duration_ms"]
        by_app[s["app_name"]] = by_app.get(s["app_name"], 0) + d
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + d
        ct = s.get("content_title")
        if ct and d >= config.MIN_CONTENT_DISPLAY_MS:
            key = (s["app_name"], ct)
            by_content[key] = by_content.get(key, 0) + d
    return by_app, by_cat, by_content


class ActivityService:
    def __init__(self, storage):
        self.storage = storage

    def derive(self, from_ts, to_ts):
        """从 events 派生活动片段（用户编辑 → 碎片过滤 → AFK 标记 → 分类）。"""
        events = self.storage.iter_events_between(from_ts, to_ts)
        # 预加载 app 元数据与规则，避免逐段 N+1 查询（数据量大时差异明显）
        apps = {r[0]: (r[0], r[1], None, r[2], r[3])
                for r in self.storage.get_all_apps()}
        rules = self.storage.get_rules()
        app_cat, seg_cat, excludes = load_edits(self.storage)
        # 心跳不参与时间轴边界：否则"停留未切换"的时长会被心跳截断而漏算。
        # lock/unlock 保留为会话边界（锁屏即结束当前活动）。
        bounds = [e for e in events if e[1] != "heartbeat"]
        # (ts, idle_ms)：用于断档截断 + 精确 AFK 判定
        hb = [(e[0], e[7]) for e in events if e[1] == "heartbeat"]
        hb_ts = [h[0] for h in hb]
        n = len(bounds)
        segs = []
        for i in range(n):
            ts, etype, _hwnd, app_id, title, _rect, _cls, _idle, is_afk, content_title = bounds[i]
            if etype in ("lock", "unlock"):
                continue
            _app = apps.get(app_id)
            _proc = _app[1] if _app else ""
            # 桌宠等按用户要求不记录（含历史数据）
            if any(k in _proc.lower() for k in config.NOISE_PROC_KEYWORDS):
                continue
            app_name = _app[3] if _app else "unknown"
            # 内容标题：抖音来自 UIA(content_title)；浏览器从窗口标题解析（MVP 白捡的内容级）
            ct = content_title
            if not ct and _proc.lower() in BROWSER_PROCS:
                ct = clean_browser_title(title)
            end = bounds[i + 1][0] if i + 1 < n else to_ts
            end = min(end, to_ts)
            # 断档修正：休眠/离开/关机时心跳会缺失。长段用区间内最后一条心跳截断，
            # 避免把空白（尤其是历史日期末段到 24:00）算给前一个应用。
            # 若整个区间无心跳（如合成数据/采集器未运行），不做修正。
            if hb_ts and end - ts > GAP_CUT_MS:
                idx = bisect.bisect_right(hb_ts, end) - 1
                if idx >= 0 and hb_ts[idx] >= ts:
                    if end - hb_ts[idx] > GAP_CUT_MS:
                        end = hb_ts[idx] + TAIL_MS
                else:
                    end = ts + TAIL_MS
            # 用户「删除该段」→ 从原始段里扣掉该时间区间（可能切成多段）
            for a, b in clip_segment(ts, end, excludes, app_name):
                segs.append({
                    "start": a, "end": b, "orig_start": ts,
                    "app_id": app_id, "app_name": app_name,
                    "title": title, "is_afk": bool(is_afk),
                    "content_title": ct, "edited": (a, b) != (ts, end),
                })
        # 用户决策 2026-10-26：
        # - 非内容活动段低于 1 分钟丢弃（应用级碎片）；
        # - 内容段无论长短都保留——其时长要计入应用统计（真实使用时间），
        #   但 <1 分钟的内容不进入"内容明细"（见 aggregate 的 by_content 过滤）。
        merged = []
        for s in segs:
            if s.get("content_title"):
                merged.append(s)
            elif s["end"] - s["start"] >= config.MIN_ACTIVITY_DURATION_MS:
                merged.append(s)
        # AFK 标记（基于 heartbeat idle 序列）：段内心跳多数 idle 超阈值才判定，
        # 避免把"看视频不操作"误判为离开。专注口径排除这些段，前台口径保留。
        for s in merged:
            i = bisect.bisect_left(hb_ts, s["start"])
            total = idle_cnt = 0
            while i < len(hb) and hb[i][0] < s["end"]:
                total += 1
                if (hb[i][1] or 0) > config.AFK_THRESHOLD_MS:
                    idle_cnt += 1
                i += 1
            # 有内容标题 = 正在看具体内容（视频/页面），即使 idle 高也不算离开；
            # 无内容且 idle 多数超阈值 → AFK（离开/发呆）
            s["afk"] = (not s.get("content_title")) and (
                bool(s["is_afk"]) or (total >= 2 and idle_cnt / total > 0.5))
            app = apps.get(s["app_id"])
            cat, sub, prod = classify_with(app, rules, s["title"])
            # 用户编辑优先于规则引擎（用户数据最高权限）
            ov = (seg_cat.get((s["app_name"], s["orig_start"]))
                  or seg_cat.get((s["app_name"], s["start"]))
                  or app_cat.get(s["app_name"]))
            if ov:
                cat, sub, prod = ov
                s["edited"] = True
            s["category"] = cat
            s["sub_category"] = sub
            s["productivity"] = prod
            s["duration_ms"] = s["end"] - s["start"]
        return merged

    def durations(self, from_ts, to_ts, exclude_afk=False):
        return aggregate(self.derive(from_ts, to_ts), exclude_afk)

    def top_apps(self, from_ts, to_ts, n=10, exclude_afk=True):
        by_app, _, _ = self.durations(from_ts, to_ts, exclude_afk)
        return sorted(by_app.items(), key=lambda x: -x[1])[:n]
