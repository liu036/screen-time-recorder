# -*- coding: utf-8 -*-
"""番茄钟核心逻辑单测（不启线程、不依赖真实时间）。

覆盖：启动/计时/暂停恢复/自动暂停/到点/休息/中止/专注报告/配置夹紧/崩溃恢复。
"""
import json
import os
import sys
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from screen_time_recorder.storage import Storage
from screen_time_recorder.pomodoro import PomodoroService


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


class FakeWall:
    def __init__(self, ms=None):
        self.ms = int(ms if ms is not None else time.time() * 1000)

    def __call__(self):
        return self.ms

    def advance(self, sec):
        self.ms += int(sec * 1000)


def make_service(st, lock_val=None):
    clock, wall = FakeClock(), FakeWall()
    notes = []
    popups = []
    lock = lock_val if lock_val is not None else {"v": 0}
    svc = PomodoroService(
        st, notifier=lambda t, b: notes.append((t, b)),
        lock_getter=lambda: lock["v"],
        popup_cb=lambda kind, t, b: popups.append((kind, t, b)),
        clock=clock, wall_ms=wall, tick_ms=1000)
    svc.test_popups = popups
    return svc, clock, wall, notes, lock, None


def seed_events(st, base_ms):
    """插入合成活动：Code 45 分钟 + 抖音 5 分钟。"""
    code = st.ensure_app("Code.exe", None, "VS Code", "Chrome_WidgetWin_1")
    dy = st.ensure_app("douyin.exe", None, "抖音", "Chrome_WidgetWin_1")
    rows = [
        (base_ms, "foreground", 1, code, "main.py", "", "", 0, 0, "", None),
        (base_ms + 45 * 60000, "foreground", 2, dy, "douyin", "", "", 0, 0, "", None),
    ]
    with st._lock:
        st._conn.executemany(
            "INSERT INTO events(ts,event_type,hwnd,app_id,window_title,rect,cls,"
            "idle_ms,is_afk,raw,content_title) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        st._conn.commit()


def test_start_and_countdown():
    st = Storage(os.path.join(tempfile.mkdtemp(), "p1.db"))
    try:
        svc, clock, wall, notes, _, _ = make_service(st)
        s = svc.start_focus(focus_minutes=50, break_minutes=10, title="写方案")
        assert s["state"] == "focus"
        assert s["planned_ms"] == 50 * 60000
        assert s["remaining_ms"] == 50 * 60000
        assert s["title"] == "写方案"
        assert notes and notes[-1][0] == "番茄钟开始"
        row = st.get_running_pomodoro()
        assert row and row["status"] == "running" and row["kind"] == "focus"

        clock.advance(60)
        wall.advance(60)
        svc.tick()
        s = svc.status()
        assert s["remaining_ms"] == 49 * 60000, s
        print("启动 / 计时 / 落库 OK")
    finally:
        st.close()


def test_pause_and_resume():
    st = Storage(os.path.join(tempfile.mkdtemp(), "p2.db"))
    try:
        svc, clock, wall, _, _, _ = make_service(st)
        svc.start_focus(focus_minutes=30, break_minutes=5)
        clock.advance(300)
        wall.advance(300)
        svc.tick()
        remain = svc.remaining_ms()
        assert remain == 25 * 60000, remain

        svc.pause("手动")
        paused_remain = svc.remaining_ms()
        clock.advance(120)          # 暂停期间时钟继续走
        wall.advance(120)
        assert svc.remaining_ms() == paused_remain, "暂停时剩余时间不应变化"
        assert svc.status()["state"] == "paused"
        assert svc.status()["interruptions"] == 1

        svc.resume()
        assert svc.status()["state"] == "focus"
        assert svc.status()["paused_ms"] == 120000
        clock.advance(60)
        wall.advance(60)
        assert svc.remaining_ms() == paused_remain - 60000
        print("暂停 / 冻结 / 恢复 / 累计 OK")
    finally:
        st.close()


def test_auto_pause_on_lock_only():
    st = Storage(os.path.join(tempfile.mkdtemp(), "p3.db"))
    try:
        lock = {"v": 0}
        svc, clock, wall, notes, _, _ = make_service(st, lock)
        svc.start_focus(focus_minutes=60, break_minutes=5)
        clock.advance(11)       # 过启动宽限期
        wall.advance(11)

        # 连续 50 分钟不碰键鼠不应触发暂停（已取消空闲检测）
        for _ in range(10):
            clock.advance(300)
            wall.advance(300)
            svc.tick()
        assert svc.status()["state"] == "focus", "空闲不应自动暂停"

        lock["v"] = 2
        svc.tick()
        s = svc.status()
        assert s["state"] == "paused" and s["paused_reason"] == "锁屏", s

        lock["v"] = 0
        svc.tick()
        assert svc.status()["state"] == "paused", "解锁不应自动继续"
        assert any(n[0] == "已自动暂停" for n in notes)

        clock.advance(180)          # 暂停期间真实时间流逝
        wall.advance(180)
        svc.resume()
        assert svc.status()["paused_ms"] == 180000, svc.status()
        print("锁屏自动暂停 / 空闲不暂停 / 不自动继续 OK")
    finally:
        st.close()


def test_complete_and_report():
    st = Storage(os.path.join(tempfile.mkdtemp(), "p4.db"))
    try:
        svc, clock, wall, notes, _, _ = make_service(st)
        seed_events(st, wall.ms)
        svc.start_focus(focus_minutes=50, break_minutes=10, title="写方案")

        clock.advance(50 * 60)
        wall.advance(50 * 60)
        svc.tick()
        s = svc.status()
        assert s["state"] == "focus_done", s
        row = st.get_pomodoro_session(s["session_id"])
        assert row["status"] == "completed"
        assert row["focused_ms"] == 50 * 60000
        report = json.loads(row["report_json"])
        assert report["rate"] is not None and report["rate"] > 0.8, report
        assert report["work_ms"] > 0 and report["fun_ms"] > 0
        assert any(n[0] == "专注结束" for n in notes)

        # 开始休息
        s = svc.start_break()
        assert s["state"] == "break" and s["planned_ms"] == 10 * 60000
        clock.advance(10 * 60)
        wall.advance(10 * 60)
        svc.tick()
        assert svc.status()["state"] == "idle"
        assert any(n[0] == "休息结束" for n in notes)
        print("到点 / 报告 / 休息 / 回空闲 OK")
    finally:
        st.close()


def test_chain_break_then_auto_focus():
    """专注 → 休息（chain）→ 休息到点自动开下一轮，继承待办与时长。"""
    st = Storage(os.path.join(tempfile.mkdtemp(), "p9.db"))
    try:
        svc, clock, wall, notes, _, _ = make_service(st)
        tid = st.add_todo("写周报")
        svc.start_focus(focus_minutes=5, break_minutes=2, todo_id=tid)

        clock.advance(5 * 60)
        wall.advance(5 * 60)
        svc.tick()
        s = svc.status()
        assert s["state"] == "focus_done", s
        assert s["next_round"]["todo_id"] == tid, s["next_round"]
        assert s["next_round"]["focus_minutes"] == 5
        assert any(p[0] == "focus_done" for p in svc.test_popups), \
            "专注结束应拉起提醒窗"

        svc.start_break(chain=True)
        s = svc.status()
        assert s["state"] == "break" and s["chain_next"] is True, s
        assert any(p[0] == "focus_done" for p in svc.test_popups)

        clock.advance(2 * 60)
        wall.advance(2 * 60)
        svc.tick()                       # 休息到点 → 自动开下一轮
        s = svc.status()
        assert s["state"] == "focus", s
        assert s["todo_id"] == tid and s["title"] == "写周报", s
        assert s["planned_ms"] == 5 * 60000
        assert s["chain_next"] is False
        assert any(p[0] == "break_done" for p in svc.test_popups), \
            "休息结束应有轻提示"
        assert any(n[0] == "休息结束" for n in notes)
        print("连续番茄：休息后自动接下一轮 / 继承待办 OK")
    finally:
        st.close()


def test_next_round_skips_break():
    st = Storage(os.path.join(tempfile.mkdtemp(), "p10.db"))
    try:
        svc, clock, wall, _, _, _ = make_service(st)
        tid = st.add_todo("写代码")
        svc.start_focus(focus_minutes=5, break_minutes=5, todo_id=tid)
        clock.advance(5 * 60)
        wall.advance(5 * 60)
        svc.tick()
        assert svc.status()["state"] == "focus_done"

        svc.next_round()                 # 不休息，直接下一轮
        s = svc.status()
        assert s["state"] == "focus" and s["todo_id"] == tid, s
        assert s["planned_ms"] == 5 * 60000
        print("直接下一轮：跳过休息 / 继承待办 OK")
    finally:
        st.close()


def test_set_config_updates_memory():
    st = Storage(os.path.join(tempfile.mkdtemp(), "p11.db"))
    try:
        svc, _, _, _, _, _ = make_service(st)
        cfg = svc.set_config(45, 8)
        assert cfg == {"focus_minutes": 45, "break_minutes": 8}, cfg
        assert st.pomodoro_config() == cfg
        s = svc.start_focus()            # 不传时长 → 用刚改的默认值
        assert s["planned_ms"] == 45 * 60000, s
        print("设置面板改时长 → 立即生效 OK")
    finally:
        st.close()


def test_abort_keeps_partial_report():
    st = Storage(os.path.join(tempfile.mkdtemp(), "p5.db"))
    try:
        svc, clock, wall, _, _, _ = make_service(st)
        seed_events(st, wall.ms)
        svc.start_focus(focus_minutes=50, break_minutes=0)
        clock.advance(20 * 60)
        wall.advance(20 * 60)
        svc.stop("manual")
        row = st.recent_pomodoro(1)[0]
        assert row["status"] == "aborted"
        assert row["focused_ms"] == 20 * 60000
        report = json.loads(row["report_json"])
        assert report["total_ms"] > 0
        assert svc.status()["state"] == "idle"
        print("中止 / 保留半程报告 OK")
    finally:
        st.close()


def test_break_zero_goes_idle():
    st = Storage(os.path.join(tempfile.mkdtemp(), "p6.db"))
    try:
        svc, clock, wall, _, _, _ = make_service(st)
        svc.start_focus(focus_minutes=5, break_minutes=0)
        clock.advance(5 * 60)
        wall.advance(5 * 60)
        svc.tick()
        assert svc.status()["state"] == "idle", svc.status()
        print("休息 0 分钟：到点直接回空闲 OK")
    finally:
        st.close()


def test_config_clamp_and_persist():
    st = Storage(os.path.join(tempfile.mkdtemp(), "p7.db"))
    try:
        cfg = st.pomodoro_config()
        assert cfg["focus_minutes"] == 25 and cfg["break_minutes"] == 5
        cfg = st.set_pomodoro_config(999, -5)
        assert cfg["focus_minutes"] == 180 and cfg["break_minutes"] == 0
        cfg = st.set_pomodoro_config(1, 999)
        assert cfg["focus_minutes"] == 5 and cfg["break_minutes"] == 60
        print("配置夹紧 / 持久化 OK")
    finally:
        st.close()


def test_recover_running_and_timeout():
    st = Storage(os.path.join(tempfile.mkdtemp(), "p8.db"))
    try:
        wall = FakeWall()
        # 未超时：应恢复
        st.add_pomodoro_session("focus", 50 * 60000, title="恢复",
                                now_ms=wall.ms - 10 * 60000)
        svc = PomodoroService(st, clock=FakeClock(), wall_ms=wall)
        svc._recover()
        s = svc.status()
        assert s["state"] == "focus" and s["title"] == "恢复"
        assert 39 * 60000 <= s["remaining_ms"] <= 40 * 60000, s

        # 已超时：标记 interrupted
        st.add_pomodoro_session("focus", 50 * 60000, title="超时",
                                now_ms=wall.ms - 2 * 3600 * 1000)
        svc2 = PomodoroService(st, clock=FakeClock(), wall_ms=wall)
        svc2._recover()
        rows = [r for r in st.recent_pomodoro(5) if r["title"] == "超时"]
        assert rows and rows[0]["status"] == "interrupted", rows
        print("崩溃恢复 / 超时标记 OK")
    finally:
        st.close()


if __name__ == "__main__":
    test_start_and_countdown()
    test_pause_and_resume()
    test_auto_pause_on_lock_only()
    test_complete_and_report()
    test_chain_break_then_auto_focus()
    test_next_round_skips_break()
    test_set_config_updates_memory()
    test_abort_keeps_partial_report()
    test_break_zero_goes_idle()
    test_config_clamp_and_persist()
    test_recover_running_and_timeout()
    print("all pomodoro tests passed")
