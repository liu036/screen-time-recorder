# -*- coding: utf-8 -*-
"""待办数据层与提醒调度的单元测试（临时库，无外部依赖）。

运行：python tests/test_todo.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.storage import Storage
from screen_time_recorder.reminder import ReminderService


def _new_storage():
    return Storage(os.path.join(tempfile.mkdtemp(), "todo_test.db"))


def _collector():
    calls = []
    return calls, (lambda title, body, ids: calls.append((title, body, ids)))


def test_crud_and_order():
    """排序：未完成 → 逾期 → 有计划时间 → 无期限；priority 夹取。"""
    st = _new_storage()
    try:
        now = int(time.time() * 1000)
        a = st.add_todo("交周报", remind_ts=now + 60000, priority=2)
        b = st.add_todo("回复邮件", due_ts=now - 3600000)
        c = st.add_todo("无期限", priority=0)

        rows = st.list_todos("today", now)
        assert [r["id"] for r in rows] == [b, a, c], rows

        assert st.update_todo(a, title="交周报（改）", priority=9) is True
        assert st.get_todo(a)["priority"] == 2, "priority 应夹取到 0..2"
        assert st.get_todo(a)["title"] == "交周报（改）"

        st.complete_todo(c)
        s = st.todo_summary(now)
        assert s["open"] == 2 and s["overdue"] == 1 and s["done_today"] == 1, s

        assert st.delete_todo(c) == 1
        assert st.get_todo(c) is None
        print("  CRUD / 排序 / 摘要 OK")
    finally:
        st.close()


def test_empty_title_rejected():
    st = _new_storage()
    try:
        for bad in ("", "   ", None):
            try:
                st.add_todo(bad)
            except ValueError:
                pass
            else:
                raise AssertionError("空标题应被拒绝")
        try:
            st.update_todo(1, title="  ")
        except ValueError:
            pass
        else:
            raise AssertionError("改成空标题应被拒绝")
        print("  空标题校验 OK")
    finally:
        st.close()


def test_reminder_once_and_merge():
    """同时到期多条 → 合并成一条；第二次 tick 不重复。"""
    st = _new_storage()
    calls, notifier = _collector()
    try:
        now = int(time.time() * 1000)
        st.add_todo("A", remind_ts=now - 1000)
        st.add_todo("B", remind_ts=now - 500)
        svc = ReminderService(st, notifier, availability=lambda: (True, ""),
                              interval_ms=999999)
        assert svc.tick(now_ms=now) == 2
        assert len(calls) == 1, calls
        assert "2 项待办到期" in calls[0][0], calls
        assert svc.tick(now_ms=now + 1000) == 0, "同一条不应重复提醒"
        assert len(calls) == 1
        print("  合并通知 / 去重 OK")
    finally:
        st.close()


def test_reminder_suppressed_then_catch_up():
    """锁屏/离开时不弹、不标记；恢复后补提醒。"""
    st = _new_storage()
    calls, notifier = _collector()
    ok = {"v": False}
    try:
        now = int(time.time() * 1000)
        tid = st.add_todo("A", remind_ts=now - 1000)
        svc = ReminderService(
            st, notifier,
            availability=lambda: (ok["v"], "" if ok["v"] else "锁屏"),
            interval_ms=999999)
        assert svc.tick(now_ms=now) == 0
        assert not calls
        assert st.get_todo(tid)["reminded_ts"] is None, "免打扰时不应标记已提醒"

        ok["v"] = True
        assert svc.tick(now_ms=now + 60000) == 1, "恢复后应补提醒"
        assert len(calls) == 1
        print("  免打扰 / 补提醒 OK")
    finally:
        st.close()


def test_reminder_snooze():
    """稍后提醒：未到不弹，到期再弹一次。"""
    st = _new_storage()
    calls, notifier = _collector()
    try:
        now = int(time.time() * 1000)
        tid = st.add_todo("A", remind_ts=now - 1000)
        svc = ReminderService(st, notifier, availability=lambda: (True, ""),
                              interval_ms=999999)
        assert svc.tick(now_ms=now) == 1
        st.snooze_todo(tid, 10, now)
        assert svc.tick(now_ms=now + 60000) == 0, "snooze 未到期不应提醒"
        assert svc.tick(now_ms=now + 11 * 60000) == 1, "snooze 到期应再提醒"
        assert len(calls) == 2
        print("  稍后提醒 OK")
    finally:
        st.close()


def test_startup_groups_overdue():
    """启动补扫：超过宽限期的历史逾期合并成一条，新的单独一条。"""
    st = _new_storage()
    calls, notifier = _collector()
    try:
        now = int(time.time() * 1000)
        st.add_todo("很久以前", remind_ts=now - 8 * 3600 * 1000)
        st.add_todo("刚刚到期", remind_ts=now - 60000)
        svc = ReminderService(st, notifier, availability=lambda: (True, ""),
                              interval_ms=999999)
        svc.tick(startup=True, now_ms=now)
        titles = [c[0] for c in calls]
        assert any("逾期" in t for t in titles), titles
        assert any("待办提醒" in t for t in titles), titles
        assert len(calls) == 2, titles
        print("  启动补扫分组 OK")
    finally:
        st.close()


if __name__ == "__main__":
    print("test_todo:")
    test_crud_and_order()
    test_empty_title_rejected()
    test_reminder_once_and_merge()
    test_reminder_suppressed_then_catch_up()
    test_reminder_snooze()
    test_startup_groups_overdue()
    print("all todo tests passed")
