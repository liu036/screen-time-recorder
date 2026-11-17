# -*- coding: utf-8 -*-
"""仪表盘「专注高亮」数据层单测。

覆盖：专注会话映射到时间线的 sf/ef、实际专注比例、<1 分钟过滤、
跨午夜裁剪、无专注时返回空数组。
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from screen_time_recorder.storage import Storage
from screen_time_recorder import dashboard


def _day_start(date):
    return int(datetime.strptime(date, "%Y-%m-%d").timestamp() * 1000)


def _seed_events(st, base_ms, minutes=90):
    app = st.ensure_app("Code.exe", None, "VS Code", "Chrome_WidgetWin_1")
    rows = [
        (base_ms, "foreground", 1, app, "main.py", "", "", 0, 0, "", None),
        (base_ms + minutes * 60000, "foreground", 1, app, "main.py",
         "", "", 0, 0, "", None),
    ]
    with st._lock:
        st._conn.executemany(
            "INSERT INTO events(ts,event_type,hwnd,app_id,window_title,rect,cls,"
            "idle_ms,is_afk,raw,content_title) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        st._conn.commit()


def test_focus_overlay():
    st = Storage(os.path.join(tempfile.mkdtemp(), "dash.db"))
    try:
        date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        day0 = _day_start(date)
        start = day0 + 10 * 3600 * 1000          # 昨天 10:00
        _seed_events(st, start)

        # 50 分钟会话，其中暂停 5 分钟 → ratio 0.9
        sid = st.add_pomodoro_session("focus", 50 * 60000,
                                      title="整理提示词规范", now_ms=start)
        st.finish_pomodoro_session(
            sid, start + 50 * 60000, 45 * 60000, 5 * 60000, 1, 0,
            "completed")

        # <1 分钟的会话不应出现在时间线上
        short = st.add_pomodoro_session("focus", 30000, title="太短",
                                        now_ms=start + 2 * 3600 * 1000)
        st.finish_pomodoro_session(short, start + 2 * 3600 * 1000 + 30000,
                                   30000, 0, 0, 0, "aborted")

        p = dashboard.day_payload(st, date)
        assert len(p["focus"]) == 1, p["focus"]
        f = p["focus"][0]
        assert abs(f["sf"] - 10 / 24) < 1e-4, f
        assert abs(f["ef"] - (10 * 60 + 50) / 1440) < 1e-4, f
        assert abs(f["ratio"] - 0.9) < 0.01, f
        assert f["title"] == "整理提示词规范"
        assert f["focused"] == "45.0分", f["focused"]
        assert f["status"] == "completed"
        print("专注会话 → sf/ef / ratio / 标题 OK")

        # 换一个没有专注的日期
        other = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        assert dashboard.day_payload(st, other)["focus"] == []
        print("无专注日期 → 空数组 OK")
    finally:
        st.close()


def test_focus_clipped_to_day():
    """跨午夜会话只画当天那一段，sf/ef 不能超出 [0,1]。"""
    st = Storage(os.path.join(tempfile.mkdtemp(), "dash2.db"))
    try:
        date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        day0 = _day_start(date)
        start = day0 + 23 * 3600 * 1000          # 昨天 23:00
        sid = st.add_pomodoro_session("focus", 2 * 3600 * 1000,
                                      title="跨午夜", now_ms=start)
        st.finish_pomodoro_session(
            sid, start + 2 * 3600 * 1000, 2 * 3600 * 1000, 0, 0, 0,
            "completed")

        f = dashboard.day_payload(st, date)["focus"][0]
        assert 0.0 <= f["sf"] < 1.0, f
        assert f["ef"] <= 1.0, f
        assert abs(f["sf"] - 23 / 24) < 1e-4, f
        assert abs(f["ef"] - 1.0) < 1e-4, f
        print("跨午夜会话裁剪到当天 OK")
    finally:
        st.close()


if __name__ == "__main__":
    test_focus_overlay()
    test_focus_clipped_to_day()
    print("all dashboard tests passed")
