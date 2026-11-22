# -*- coding: utf-8 -*-
"""番茄钟结束报告（从 pomodoro.py 抽出的纯计算部分）。

用采集到的真实应用/内容分布，回答"这 25 分钟到底干了什么"。
和状态机分离后，报告逻辑可以单独测试，也不会让 pomodoro.py 继续变长。
"""
def human(ms):
    """毫秒 → 中文时长（报告文案用）。"""
    s = int(ms // 1000)
    if s < 60:
        return "%d 秒" % s
    m, sec = divmod(s, 60)
    if m < 60:
        return "%d 分 %d 秒" % (m, sec) if sec else "%d 分钟" % m
    h, m = divmod(m, 60)
    return "%d 小时 %d 分" % (h, m) if m else "%d 小时" % h


def build_report(storage, start_ts, end_ts, focused_ms):
    """统计 [start_ts, end_ts] 的真实分布，返回可 JSON 化的 dict。

    采集覆盖不足一半时 rate 为 None：刚启动/采集暂停时 total 会很小，
    直接算会得出误导性的 0%。
    """
    out = {"start_ts": start_ts, "end_ts": end_ts,
           "focused_ms": int(focused_ms), "rate": None,
           "by_cat": [], "top_apps": [], "top_content": [],
           "work_ms": 0, "fun_ms": 0, "total_ms": 0, "text": ""}
    try:
        # 延迟导入：pomodoro 只被待办窗口用来取 fmt_mmss，没必要连带加载采集栈
        from .classifier import ActivityService
        svc = ActivityService(storage)
        by_app, by_cat, by_content = svc.durations(start_ts, end_ts)
    except Exception as e:
        print("[pomodoro] 生成报告失败: %s" % e)
        return out

    total = sum(by_app.values())
    work = by_cat.get("工作", 0)
    # 影音（B 站/抖音等）也是消遣，不并进来会让专注报告少算一大块
    fun = by_cat.get("娱乐", 0) + by_cat.get("影音", 0)
    out["total_ms"] = total
    out["work_ms"] = work
    out["fun_ms"] = fun
    if focused_ms > 0 and total >= focused_ms * 0.5:
        out["rate"] = max(0.0, min(1.0, work / float(focused_ms)))
    out["by_cat"] = sorted(by_cat.items(), key=lambda x: -x[1])[:6]
    out["top_apps"] = sorted(by_app.items(), key=lambda x: -x[1])[:5]
    out["top_content"] = [
        {"app": a, "title": t, "ms": d}
        for (a, t), d in sorted(by_content.items(),
                                key=lambda x: -x[1])[:3]]

    rate = out["rate"]
    if rate is None:
        out["text"] = "这段时间没有可用的采集数据。"
    elif rate >= 0.8:
        out["text"] = "这次很扎实，几乎全程在工作。"
    elif rate >= 0.5:
        out["text"] = "基本在状态，有 %s 分给了别的。" % human(fun or 0)
    else:
        out["text"] = "这次注意力比较散，%s 在娱乐/浏览上。" % human(
            max(0, total - work))
    return out
