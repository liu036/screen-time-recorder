# -*- coding: utf-8 -*-
"""抖音内容级：字段迁移 + 统计切分验证（合成数据，不污染真实库）。"""
import sys
import os
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.storage import Storage
from screen_time_recorder.classifier import ActivityService
from screen_time_recorder import douyin_content


def main():
    # 1) 导入与字段迁移检查
    print("[1] import + 字段迁移")
    assert douyin_content._HAS_UIA, "uiautomation 未就绪"
    tmp = tempfile.mktemp(suffix=".db")
    st = Storage(tmp)
    cols = [r[1] for r in st._conn.execute("PRAGMA table_info(events)").fetchall()]
    assert "content_title" in cols, f"content_title 缺失: {cols}"
    print("    events 含 content_title ✅")

    # 2) 合成抖音前台 + 两个 content 切换 + 切到 Firefox
    print("[2] 合成抖音内容级事件 + derive 切分")
    aid = st.ensure_app("douyin.exe", None, "抖音", "Chrome_WidgetWin_1")
    fx_aid = st.ensure_app("firefox.exe", None, "Mozilla Firefox", "MozillaWindowClass")
    now = int(time.time() * 1000)
    rows = [
        (now, "foreground", 1, aid, "douyin", "", "", 0, 0, "", None),
        (now + 100000, "content", 1, aid, "", "", "", 0, 0, "", "视频A @作者A"),
        (now + 400000, "content", 1, aid, "", "", "", 0, 0, "", "视频B @作者B"),
        (now + 600000, "foreground", 2, fx_aid, "Mozilla Firefox", "", "", 0, 0, "", None),
    ]
    st._conn.executemany(
        "INSERT INTO events(ts,event_type,hwnd,app_id,window_title,rect,cls,"
        "idle_ms,is_afk,raw,content_title) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    st._conn.commit()

    svc = ActivityService(st)
    by_app, by_cat, by_content = svc.durations(now - 1000, now + 700000)
    print("    by_app     =", {k: round(v / 1000) for k, v in by_app.items()})
    print("    by_content =", {k: round(v / 1000) for k, v in by_content.items()})

    # 期望：抖音 600s；Firefox 100s；内容A 300s / 内容B 200s
    assert abs(by_app["抖音"] - 600000) < 1000, by_app
    assert abs(by_app["Mozilla Firefox"] - 100000) < 1000, by_app
    assert abs(by_content[("抖音", "视频A @作者A")] - 300000) < 1000, by_content
    assert abs(by_content[("抖音", "视频B @作者B")] - 200000) < 1000, by_content
    print("    内容级切分与聚合 ✅")

    # 3) fetch_douyin_content 在无效窗口下安全返回 None
    print("[3] fetch_douyin_content(0) 安全返回")
    assert douyin_content.fetch_douyin_content(0) is None
    print("    无效 hwnd 返回 None ✅（不抛异常）")

    st.close()
    os.remove(tmp)
    print("\n全部通过 ✅")


if __name__ == "__main__":
    main()
