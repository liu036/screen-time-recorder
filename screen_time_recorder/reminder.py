# -*- coding: utf-8 -*-
"""待办提醒调度（主进程内的轻量线程）。

设计要点：
- 每 20s 扫一次到期待办；命中后**先落库再弹通知**，避免异常重启后重复轰炸；
- 锁屏 / 长时间无操作时不打扰，恢复后因为 reminded_ts 没写，会自然补提醒；
- 启动补扫时，超过宽限期（默认 6h）的历史逾期合并成一条，不逐条弹；
- 同一次 tick 命中多条时合并成一条通知，避免刷屏。
"""
import threading
import time
from datetime import datetime

from . import config


class ReminderService(threading.Thread):
    def __init__(self, storage, notifier, availability=None,
                 interval_ms=None, badge=None):
        """
        storage:      Storage
        notifier:     callable(title, body, todo_ids) -> bool
        availability: callable() -> (ok: bool, reason: str)；False 表示此刻不宜打扰
        badge:        callable(summary: dict)；可选，用于刷新托盘提示
        """
        super().__init__(daemon=True, name="todo-reminder")
        self.storage = storage
        self.notifier = notifier
        self.availability = availability or (lambda: (True, ""))
        self.interval = (interval_ms or config.REMINDER_INTERVAL_MS) / 1000.0
        self.badge = badge
        self._stop = threading.Event()
        self._startup_done = False
        self._last_reason = None

    def stop(self):
        self._stop.set()

    def run(self):
        # 等托盘/采集初始化完再开始，避免启动瞬间抢资源
        self._stop.wait(5)
        while not self._stop.is_set():
            try:
                self.tick(startup=not self._startup_done)
                self._startup_done = True
                if self.badge:
                    self.badge(self.storage.todo_summary())
            except Exception as e:
                print(f"[reminder] tick error: {e}")
            self._stop.wait(self.interval)

    # ---- 单次扫描（测试可直接调用）----
    def tick(self, startup=False, now_ms=None):
        ok, reason = self.availability()
        if not ok:
            # 只在状态变化时打日志，避免每 20s 刷一行
            if reason != self._last_reason:
                print(f"[reminder] 暂不提醒（{reason}）")
                self._last_reason = reason
            return 0
        self._last_reason = None

        now = int(now_ms or time.time() * 1000)
        items = self.storage.due_todos(now)
        if not items:
            return 0

        # 先写库，再弹：即便通知失败也不会重复提醒
        for it in items:
            self.storage.mark_reminded(it["id"], now)

        if startup:
            cutoff = now - config.REMINDER_STARTUP_GRACE_MS
            overdue = [i for i in items if (i["remind_ts"] or 0) < cutoff]
            fresh = [i for i in items if (i["remind_ts"] or 0) >= cutoff]
            if overdue:
                self._notify_overdue(overdue)
            if fresh:
                self._notify_due(fresh)
        else:
            self._notify_due(items)
        return len(items)

    # ---- 通知文案 ----
    def _notify_due(self, items):
        if len(items) == 1:
            it = items[0]
            title = f"待办提醒 · {_hhmm(it.get('remind_ts'))}"
            body = it["title"]
            if it.get("note"):
                body += "\n" + it["note"][:120]
        else:
            title = f"{len(items)} 项待办到期"
            body = _bullet_list(items)
        self.notifier(title, body, [i["id"] for i in items])

    def _notify_overdue(self, items):
        title = f"有 {len(items)} 项待办已逾期"
        self.notifier(title, _bullet_list(items), [i["id"] for i in items])


def _bullet_list(items, limit=5):
    lines = ["· " + i["title"] for i in items[:limit]]
    if len(items) > limit:
        lines.append(f"… 还有 {len(items) - limit} 项")
    return "\n".join(lines)


def _hhmm(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts / 1000.0).strftime("%H:%M")
