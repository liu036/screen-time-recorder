# -*- coding: utf-8 -*-
"""核心逻辑自动化测试。

可用 pytest 运行，也可 `python tests/test_core.py` 直接运行。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.storage import Storage
from screen_time_recorder.classifier import ActivityService, classify_with


def _new_storage():
    d = tempfile.mkdtemp()
    return Storage(os.path.join(d, "test.db"))


def _insert(st, rows):
    st._conn.executemany(
        "INSERT INTO events(ts,event_type,hwnd,app_id,window_title,rect,cls,"
        "idle_ms,is_afk,raw,content_title) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    st._conn.commit()


def test_schema_migration():
    """旧库/新库都应具备 content_title 列。"""
    st = _new_storage()
    try:
        cols = [r[1] for r in st._conn.execute("PRAGMA table_info(events)").fetchall()]
        assert "content_title" in cols
    finally:
        st.close()


def test_content_split_and_aggregate():
    """content 事件切分活动段，并按内容聚合时长。"""
    st = _new_storage()
    try:
        aid = st.ensure_app("douyin.exe", None, "抖音", "Chrome_WidgetWin_1")
        fx = st.ensure_app("firefox.exe", None, "Mozilla Firefox", "MozillaWindowClass")
        now = int(time.time() * 1000)
        _insert(st, [
            (now, "foreground", 1, aid, "douyin", "", "", 0, 0, "", None),
            (now + 100000, "content", 1, aid, "", "", "", 0, 0, "", "视频A @作者A"),
            (now + 400000, "content", 1, aid, "", "", "", 0, 0, "", "视频B @作者B"),
            (now + 600000, "foreground", 2, fx, "Firefox", "", "", 0, 0, "", None),
        ])
        by_app, _, by_content = ActivityService(st).durations(now - 1000, now + 700000)
        assert abs(by_app["抖音"] - 600000) < 1000
        assert abs(by_app["Mozilla Firefox"] - 100000) < 1000
        assert abs(by_content[("抖音", "视频A @作者A")] - 300000) < 1000
        assert abs(by_content[("抖音", "视频B @作者B")] - 200000) < 1000
    finally:
        st.close()


def test_short_segment_rules():
    """内容段 <1 分钟：时长计入应用统计，但不进内容明细；非内容段 <1 分钟丢弃。"""
    st = _new_storage()
    try:
        aid = st.ensure_app("douyin.exe", None, "抖音", "Chrome_WidgetWin_1")
        now = int(time.time() * 1000)
        _insert(st, [
            (now, "foreground", 1, aid, "douyin", "", "", 0, 0, "", None),
            (now + 100000, "content", 1, aid, "", "", "", 0, 0, "", "视频A @作者A"),
            (now + 101000, "content", 1, aid, "", "", "", 0, 0, "", "视频B @作者B"),
            (now + 200000, "foreground", 2, aid, "other", "", "", 0, 0, "", None),
        ])
        by_app, _, by_content = ActivityService(st).durations(now - 1000, now + 201000)
        # 首段 100s + 视频A 1s + 视频B 99s 都计入抖音（共 200s）
        assert abs(by_app["抖音"] - 200000) < 1000
        # 视频A <1 分钟：不进内容明细；视频B 99s：进入
        assert ("抖音", "视频A @作者A") not in by_content
        assert ("抖音", "视频B @作者B") in by_content
        # 结尾 other 段仅 1s 且非内容 -> 丢弃
        assert len(by_app) == 1
    finally:
        st.close()


def test_delete_range():
    """按区间删除原始事件与快照。"""
    st = _new_storage()
    try:
        aid = st.ensure_app("douyin.exe", None, "抖音", "Chrome_WidgetWin_1")
        now = int(time.time() * 1000)
        _insert(st, [
            (now, "foreground", 1, aid, "douyin", "", "", 0, 0, "", None),
            (now + 1000, "content", 1, aid, "", "", "", 0, 0, "", "x @y"),
        ])
        n_e, n_s = st.delete_range(now - 1, now + 2000)
        assert n_e == 2
        assert n_s == 0
        assert st.count_events() == 0
    finally:
        st.close()


def test_classify_with():
    """规则分类：抖音 → 娱乐 / distracting。"""
    st = _new_storage()
    try:
        app = (1, "douyin.exe", None, "抖音", "Chrome_WidgetWin_1")
        cat, _sub, prod = classify_with(app, st.get_rules(), "")
        assert cat == "娱乐"
        assert prod == "distracting"
    finally:
        st.close()


def test_browser_title_clean():
    """浏览器窗口标题解析为页面标题；首页/应用名/播放器噪音返回空。"""
    from screen_time_recorder.classifier import clean_browser_title
    assert clean_browser_title(
        "谁才是底层百姓真正的天？第39回_哔哩哔哩_bilibili — Mozilla Firefox"
    ) == "谁才是底层百姓真正的天？第39回_哔哩哔哩_bilibili"
    assert clean_browser_title("某页面 - Google Chrome") == "某页面"
    assert clean_browser_title("Mozilla Firefox") == ""
    assert clean_browser_title(
        "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili — Mozilla Firefox") == ""
    assert clean_browser_title(
        "上次播放至：00:03 — Mozilla Firefox") == ""
    assert clean_browser_title("视频加载中… — Mozilla Firefox") == ""


def test_afk_marking():
    """段内心跳多数 idle 超阈值 → afk=True；低 idle → False。"""
    st = _new_storage()
    try:
        aid = st.ensure_app("douyin.exe", None, "抖音", "Chrome_WidgetWin_1")
        now = int(time.time() * 1000)

        def row(ts, etype, idle):
            return (ts, etype, 0 if etype == "heartbeat" else 1,
                    0 if etype == "heartbeat" else aid, "", "", "",
                    idle, 1 if idle > 90000 else 0, "", None)

        # 段 A：3 条心跳 idle 均超阈值
        _insert(st, [
            row(now, "foreground", 0),
            row(now + 30000, "heartbeat", 120000),
            row(now + 90000, "heartbeat", 150000),
            row(now + 150000, "heartbeat", 150000),
            row(now + 240000, "foreground", 0),
            # 段 B：心跳 idle 很低（真实使用）
            row(now + 270000, "heartbeat", 4000),
            row(now + 330000, "heartbeat", 5000),
            row(now + 390000, "heartbeat", 6000),
            row(now + 480000, "foreground", 0),
        ])
        segs = ActivityService(st).derive(now - 1000, now + 480000)
        assert segs[0]["afk"] is True
        assert segs[1]["afk"] is False
    finally:
        st.close()


def test_edit_category_and_exclude():
    """用户编辑层：应用级/单段改分类、删除某段、撤销。"""
    from screen_time_recorder import dashboard
    st = _new_storage()
    try:
        fx = st.ensure_app("firefox.exe", None, "Mozilla Firefox", "MozillaWindowClass")
        now = int(time.time() * 1000)
        _insert(st, [
            (now, "foreground", 1, fx, "页面一 — Mozilla Firefox", "", "", 0, 0, "", None),
            (now + 100000, "foreground", 1, fx, "页面二 — Mozilla Firefox", "", "", 0, 0, "", None),
            (now + 200000, "foreground", 1, fx, "页面三 — Mozilla Firefox", "", "", 0, 0, "", None),
        ])
        svc = ActivityService(st)
        f, t = now - 1000, now + 300000
        _, by_cat, _ = svc.durations(f, t)
        assert by_cat.get("浏览网页", 0) > 250000

        # 1) 应用级改分类 → 全部段变娱乐
        dashboard.set_category(st, "Mozilla Firefox", now, "娱乐", scope="app")
        _, by_cat2, _ = svc.durations(f, t)
        assert "浏览网页" not in by_cat2
        assert abs(by_cat2["娱乐"] - 300000) < 1500

        for e in st.list_edits():
            st.delete_edit(e[0])

        # 2) 单段改分类 → 只有第一段变工作
        dashboard.set_category(st, "Mozilla Firefox", now, "工作", scope="segment")
        segs = svc.derive(f, t)
        assert segs[0]["category"] == "工作"
        assert segs[1]["category"] == "浏览网页"

        for e in st.list_edits():
            st.delete_edit(e[0])

        # 3) 删除某段 → 时长减少 100s
        dashboard.exclude_segment(st, "Mozilla Firefox", now, now + 100000)
        segs2 = svc.derive(f, t)
        assert len(segs2) == 2
        assert abs(sum(x["duration_ms"] for x in segs2) - 200000) < 1500
    finally:
        st.close()


if __name__ == "__main__":
    test_schema_migration()
    test_content_split_and_aggregate()
    test_short_segment_rules()
    test_delete_range()
    test_classify_with()
    test_browser_title_clean()
    test_afk_marking()
    test_edit_category_and_exclude()
    print("all tests passed")
