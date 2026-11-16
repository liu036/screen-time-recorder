# -*- coding: utf-8 -*-
"""番茄钟服务（主进程内的常驻线程）。

设计要点：
- 计时用 monotonic，落库用 wall clock；暂停时长单独累计，不受系统时间调整影响；
- 到点不自动休息：进入 focus_done，等用户选择（超时才回空闲）；
- 连续番茄（chain）：专注结束后选「休息并继续」→ 休息到点立即自动开下一轮，
  继承上一轮的待办、标题与时长，形成 专注→休息→专注 的闭环；
- 锁屏 / 连续 5 分钟无输入 → 自动暂停；恢复活跃只提示、不自动继续；
- 结束时用该时间区间向 ActivityService 要真实应用/内容分布，生成专注报告；
- 会话状态实时落库，进程崩溃后可恢复或标记 interrupted。
"""
import json
import threading
import time

from . import config


def _clamp(v, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def fmt_mmss(ms):
    """毫秒 → MM:SS（超过 1 小时用 H:MM:SS）。"""
    s = max(0, int(ms // 1000))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, sec)
    return "%02d:%02d" % (m, sec)


def _human(ms):
    s = int(ms // 1000)
    if s < 60:
        return "%d 秒" % s
    m, sec = divmod(s, 60)
    if m < 60:
        return "%d 分 %d 秒" % (m, sec) if sec else "%d 分钟" % m
    h, m = divmod(m, 60)
    return "%d 小时 %d 分" % (h, m) if m else "%d 小时" % h


class PomodoroService(threading.Thread):
    """focus / paused / focus_done / break / idle 五态。"""

    def __init__(self, storage, notifier=None, state_cb=None,
                 lock_getter=None, idle_getter=None, popup_cb=None,
                 clock=None, wall_ms=None, tick_ms=None):
        """
        storage:      Storage
        notifier:     callable(title, body) -> bool（托盘气泡）
        state_cb:     callable(status_dict)，用于刷新托盘 tooltip
        lock_getter:  callable() -> int（2 = 锁屏）
        idle_getter:  callable() -> int（毫秒）
        popup_cb:     callable(kind, title, body)，拉起置顶提醒小窗
        clock:        callable() -> 秒，默认 time.monotonic（测试可注入）
        wall_ms:      callable() -> 毫秒，默认 time.time
        """
        super().__init__(daemon=True, name="pomodoro")
        self.storage = storage
        self.notifier = notifier or (lambda t, b: False)
        self.popup_cb = popup_cb
        self.state_cb = state_cb
        self.lock_getter = lock_getter or (lambda: 0)
        self.idle_getter = idle_getter or (lambda: 0)
        self.clock = clock or time.monotonic
        self.wall_ms = wall_ms or (lambda: int(time.time() * 1000))
        self.interval = (tick_ms or config.POMODORO_TICK_MS) / 1000.0

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state = "idle"          # idle/focus/paused/focus_done/break
        self._session = None          # 当前会话 dict（DB 行）
        self._start_mono = 0.0         # 本次计时起点（focus 或 break）
        self._accum_pause_ms = 0       # 累计暂停
        self._pause_started_mono = 0.0
        self._pause_reason = ""
        self._paused_by_auto = False
        self._afk_ms = 0
        self._interruptions = 0
        self._done_since_mono = None   # 进入 focus_done 的时间
        self._focus_minutes = config.POMODORO_DEFAULT_FOCUS_MIN
        self._break_minutes = config.POMODORO_DEFAULT_BREAK_MIN
        self._last_state = None
        self._last_cb_mono = 0.0
        self._paused_hint_sent = False
        self._grace_until_mono = 0.0   # 启动/恢复宽限期
        self._chain_next = False       # 休息结束后是否自动开始下一轮
        self._next_round = None        # 下一轮的参数（专注结束时就定好）

        try:
            cfg = storage.pomodoro_config()
            self._focus_minutes = cfg["focus_minutes"]
            self._break_minutes = cfg["break_minutes"]
        except Exception:
            pass

    # ---------- 线程 ----------
    def shutdown(self):
        """停止后台线程（区别于 stop：stop 是中止当前会话）。"""
        self._stop.set()

    def run(self):
        self._stop.wait(3)          # 等托盘/采集初始化
        try:
            self._recover()
        except Exception as e:
            print("[pomodoro] 恢复失败: %s" % e)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                print("[pomodoro] tick error: %s" % e)
            self._stop.wait(self.interval)

    # ---------- 单次 tick（测试可直接调）----------
    def tick(self):
        changed = False
        notify = None
        auto_next = None
        cb = False

        with self._lock:
            if self._state == "focus":
                # 1) 锁屏 / 长时间离开 → 自动暂停
                reason = self._auto_pause_reason()
                if reason:
                    self._pause_locked(reason)
                    changed = True
                else:
                    # 2) 到点
                    if self._elapsed_ms_locked() >= int(self._session["planned_ms"]):
                        notify, changed = self._complete_focus_locked()
            elif self._state == "paused":
                if self._paused_by_auto and not self._auto_pause_reason():
                    if not self._paused_hint_sent:
                        self._paused_hint_sent = True
                        notify = ("已自动暂停",
                                  "你离开了一会儿，番茄钟已暂停。点托盘「番茄钟 · 继续」接上。")
                    changed = True   # 让 tooltip 更新提示
            elif self._state == "focus_done":
                if (self._done_since_mono is not None and
                        (self.clock() - self._done_since_mono) * 1000
                        >= config.POMODORO_DONE_TIMEOUT_MS):
                    self._state = "idle"
                    self._session = None
                    self._done_since_mono = None
                    self._next_round = None
                    self._chain_next = False
                    changed = True
            elif self._state == "break":
                if self._elapsed_ms_locked() >= int(self._session["planned_ms"]):
                    chain = bool(self._chain_next and self._next_round)
                    if chain:
                        auto_next = dict(self._next_round)
                    self._finish_break_locked()
                    self._chain_next = False
                    changed = True
                    if chain:
                        notify = ("休息结束",
                                  "已自动开始下一个番茄：专注 %d 分钟。"
                                  % auto_next["focus_minutes"],
                                  "break_done")
                    else:
                        notify = ("休息结束",
                                  "准备好了就点「番茄钟」开始下一个。",
                                  "break_done")

            if changed or self._state != self._last_state:
                self._last_state = self._state
                cb = True
            now_mono = self.clock()
            if now_mono - self._last_cb_mono >= config.POMODORO_TIP_INTERVAL_MS / 1000.0:
                self._last_cb_mono = now_mono
                cb = True

        # 通知与自动开轮必须在锁外：start_focus 自己会拿同一把不可重入锁
        if notify:
            self._announce(*notify)
        if auto_next:
            try:
                self.start_focus(**auto_next)
            except Exception as e:
                print("[pomodoro] 自动开始下一轮失败: %s" % e)
        if cb and self.state_cb:
            try:
                self.state_cb(self.status())
            except Exception:
                pass

    # ---------- 控制 ----------
    def start_focus(self, focus_minutes=None, break_minutes=None,
                    todo_id=None, title=None):
        """开始专注。已有会话在跑时先中止旧的。

        注意：不要命名为 start() —— 那会覆盖 threading.Thread.start()，
        导致后台 tick 线程永远不启动。
        """
        fm = _clamp(focus_minutes if focus_minutes is not None
                    else self._focus_minutes,
                    config.POMODORO_FOCUS_MIN, config.POMODORO_FOCUS_MAX)
        bm = _clamp(break_minutes if break_minutes is not None
                    else self._break_minutes,
                    config.POMODORO_BREAK_MIN, config.POMODORO_BREAK_MAX)

        with self._lock:
            if self._state != "idle":
                self._abort_locked("replaced")
            if not title and todo_id is not None:
                todo = self.storage.get_todo(todo_id)
                title = todo["title"] if todo else ""
            planned = fm * 60000
            sid = self.storage.add_pomodoro_session(
                "focus", planned, todo_id=todo_id, title=title or "",
                now_ms=self.wall_ms())
            self._session = self.storage.get_pomodoro_session(sid)
            self._state = "focus"
            self._start_mono = self.clock()
            self._accum_pause_ms = 0
            self._pause_started_mono = 0.0
            self._pause_reason = ""
            self._paused_by_auto = False
            self._afk_ms = 0
            self._interruptions = 0
            self._done_since_mono = None
            self._paused_hint_sent = False
            self._grace_until_mono = (self.clock()
                                      + config.POMODORO_START_GRACE_S)
            self._focus_minutes, self._break_minutes = fm, bm
            self._chain_next = False
            self._next_round = None
            self._last_state = "focus"
        try:
            self.storage.set_pomodoro_config(fm, bm)
        except Exception:
            pass
        label = title or "自由专注"
        self._notify("番茄钟开始", "专注 %d 分钟 · %s" % (fm, label))
        self._emit_state()
        return self.status()

    def pause(self, reason="manual"):
        notify = None
        with self._lock:
            if self._state != "focus":
                return self.status()
            self._pause_locked(reason)
        self._emit_state()
        return self.status()

    def resume(self):
        with self._lock:
            if self._state != "paused":
                return self.status()
            now = self.clock()
            if self._pause_started_mono:
                paused = int((now - self._pause_started_mono) * 1000)
                self._accum_pause_ms += paused
                if self._paused_by_auto:
                    self._afk_ms += paused
            self._state = "focus"
            self._pause_started_mono = 0.0
            self._pause_reason = ""
            self._paused_by_auto = False
            self._paused_hint_sent = False
            self._grace_until_mono = (self.clock()
                                      + config.POMODORO_START_GRACE_S)
            self._persist_progress_locked()
        self._emit_state()
        return self.status()

    def stop(self, reason="manual"):
        """中止当前会话（专注会生成截至此刻的报告）。"""
        with self._lock:
            if self._state == "idle":
                return self.status()
            self._abort_locked(reason)
        self._emit_state()
        return self.status()

    def start_break(self, chain=False):
        """开始休息。chain=True 时休息到点自动开始下一轮专注。"""
        with self._lock:
            if self._state not in ("focus_done", "idle"):
                return self.status()
            self._chain_next = bool(chain)
            if chain and not self._next_round:
                # 理论上 _next_round 在专注结束时就已生成；兜底用当前配置
                self._next_round = {
                    "focus_minutes": self._focus_minutes,
                    "break_minutes": self._break_minutes,
                }
            bm = self._break_minutes
            if bm <= 0:
                # 没有休息时长：chain 直接落到下一轮
                immediate = (dict(self._next_round)
                             if (chain and self._next_round) else None)
                self._state = "idle"
                self._session = None
                self._chain_next = False
            else:
                immediate = None
                planned = bm * 60000
                sid = self.storage.add_pomodoro_session(
                    "break", planned, title="休息", now_ms=self.wall_ms())
                self._session = self.storage.get_pomodoro_session(sid)
                self._state = "break"
                self._start_mono = self.clock()
                self._accum_pause_ms = 0
                self._pause_started_mono = 0.0
                self._afk_ms = 0
                self._interruptions = 0
                self._done_since_mono = None
                self._last_state = "break"
        if immediate:
            return self.start_focus(**immediate)
        self._emit_state()
        return self.status()

    def next_round(self):
        """跳过休息，沿用上一轮的待办与时长直接开下一轮。"""
        with self._lock:
            if self._state != "focus_done":
                return self.status()
            nxt = dict(self._next_round or {})
            self._state = "idle"
            self._session = None
            self._done_since_mono = None
            self._chain_next = False
            self._last_state = "idle"
        if not nxt:
            nxt = {"focus_minutes": self._focus_minutes,
                   "break_minutes": self._break_minutes}
        return self.start_focus(**nxt)

    def set_config(self, focus_minutes=None, break_minutes=None):
        """更新默认时长（设置面板改了就立即生效，不必等下一次 start）。"""
        with self._lock:
            fm = _clamp(focus_minutes if focus_minutes is not None
                        else self._focus_minutes,
                        config.POMODORO_FOCUS_MIN, config.POMODORO_FOCUS_MAX)
            bm = _clamp(break_minutes if break_minutes is not None
                        else self._break_minutes,
                        config.POMODORO_BREAK_MIN, config.POMODORO_BREAK_MAX)
            self._focus_minutes, self._break_minutes = fm, bm
        try:
            self.storage.set_pomodoro_config(fm, bm)
        except Exception:
            pass
        self._emit_state()
        return {"focus_minutes": fm, "break_minutes": bm}

    def skip_break(self):
        """跳过休息 / 结束 focus_done，回到空闲（不会自动开下一轮）。"""
        with self._lock:
            if self._state == "break" and self._session:
                self._finish_break_locked()
            else:
                self._state = "idle"
                self._session = None
                self._done_since_mono = None
                self._chain_next = False
                self._next_round = None
        self._emit_state()
        return self.status()

    def ack_setup(self):
        """窗口已展开设置面板，清掉托盘留下的标记。"""
        try:
            self.storage.delete_setting("pomodoro.open_setup")
        except Exception:
            pass

    # ---------- 状态查询 ----------
    def status(self):
        with self._lock:
            return self._status_locked()

    def _status_locked(self):
        cfg = {"focus_minutes": self._focus_minutes,
               "break_minutes": self._break_minutes}
        out = {
            "state": self._state,
            "server_ms": self.wall_ms(),
            "config": cfg,
            "today": self.storage.pomodoro_summary(self.wall_ms()),
            "open_setup": self.storage.get_setting("pomodoro.open_setup") == "1",
            "chain_next": bool(self._chain_next),
            "next_round": dict(self._next_round or {}),
        }
        if self._session:
            elapsed = self._elapsed_ms_locked()
            planned = int(self._session["planned_ms"])
            out.update({
                "session_id": self._session["id"],
                "kind": self._session["kind"],
                "todo_id": self._session["todo_id"],
                "title": self._session["title"] or "",
                "planned_ms": planned,
                "elapsed_ms": min(elapsed, planned),
                "remaining_ms": max(0, planned - elapsed),
                "focused_ms": min(elapsed, planned),
                "paused_ms": self._paused_total_locked(),
                "interruptions": self._interruptions,
                "paused_reason": self._pause_reason,
                "break_minutes": self._break_minutes,
                "focus_minutes": self._focus_minutes,
            })
        else:
            out.update({"session_id": None, "remaining_ms": 0,
                        "elapsed_ms": 0, "planned_ms": 0,
                        "title": "", "todo_id": None, "kind": None,
                        "paused_ms": 0, "interruptions": 0,
                        "paused_reason": ""})
        return out

    def remaining_ms(self):
        with self._lock:
            if not self._session:
                return 0
            return max(0, int(self._session["planned_ms"])
                       - self._elapsed_ms_locked())

    # ---------- 内部 ----------
    def _elapsed_ms_locked(self):
        if not self._session:
            return 0
        ref = (self._pause_started_mono if self._state == "paused"
               else self.clock())
        return max(0, int((ref - self._start_mono) * 1000)
                   - self._accum_pause_ms)

    def _paused_total_locked(self):
        total = self._accum_pause_ms
        if self._state == "paused" and self._pause_started_mono:
            total += int((self.clock() - self._pause_started_mono) * 1000)
        return max(0, total)

    def _auto_pause_reason(self):
        # 刚开始/刚恢复时不要因为"之前的空闲"立刻暂停
        if self.clock() < self._grace_until_mono:
            return ""
        try:
            if int(self.lock_getter() or 0) == 2:
                return "锁屏"
        except Exception:
            pass
        try:
            idle = int(self.idle_getter() or 0)
            if idle >= config.POMODORO_IDLE_LIMIT_MS:
                return "离开 %d 分钟" % (idle // 60000)
        except Exception:
            pass
        return ""

    def _pause_locked(self, reason):
        self._state = "paused"
        self._pause_started_mono = self.clock()
        self._pause_reason = reason or "手动"
        self._paused_by_auto = reason in ("锁屏",) or reason.startswith("离开")
        self._paused_hint_sent = False
        self._interruptions += 1
        self._persist_progress_locked()
        self._last_state = "paused"

    def _persist_progress_locked(self):
        if not self._session:
            return
        try:
            self.storage.update_pomodoro_session(
                self._session["id"],
                focused_ms=min(self._elapsed_ms_locked(),
                               int(self._session["planned_ms"])),
                paused_ms=self._paused_total_locked(),
                interruptions=self._interruptions,
                afk_ms=self._afk_ms)
        except Exception:
            pass

    def _complete_focus_locked(self):
        """专注到点：结算 → 生成报告 → 进入 focus_done。"""
        planned = int(self._session["planned_ms"])
        focused = min(self._elapsed_ms_locked(), planned)
        start_ts = int(self._session["started_ts"])
        end_ts = self.wall_ms()      # 用实际结束时间，报告覆盖完整跨度
        report = self._build_report(start_ts, end_ts, focused)
        try:
            self.storage.finish_pomodoro_session(
                self._session["id"], end_ts, focused,
                self._paused_total_locked(), self._interruptions,
                self._afk_ms, "completed",
                json.dumps(report, ensure_ascii=False))
        except Exception as e:
            print("[pomodoro] 写入完成状态失败: %s" % e)
        self._session = self.storage.get_pomodoro_session(
            self._session["id"]) or self._session
        self._done_since_mono = self.clock()
        # 提前定好下一轮参数：从本轮会话继承待办、标题与时长
        done_minutes = max(1, planned // 60000)
        self._next_round = {
            "focus_minutes": done_minutes,
            "break_minutes": self._break_minutes,
            "todo_id": self._session.get("todo_id"),
            "title": self._session.get("title") or "",
        }

        rate = report.get("rate")
        rate_txt = ("专注率 %d%%" % round(rate * 100)) if rate is not None else ""
        if self._break_minutes > 0:
            self._state = "focus_done"
            body = "已完成 %d 分钟%s。休息 %d 分钟后自动开始下一轮。" % (
                done_minutes,
                ("，" + rate_txt) if rate_txt else "",
                self._break_minutes)
            title = "专注结束"
        else:
            self._state = "idle"
            self._session = None
            body = "已完成 %d 分钟%s。" % (
                done_minutes,
                ("，" + rate_txt) if rate_txt else "")
            title = "专注完成"
        self._last_state = self._state
        return (title, body, "focus_done"), True

    def _finish_break_locked(self):
        if not self._session:
            self._state = "idle"
            return
        planned = int(self._session["planned_ms"])
        end_ts = int(self._session["started_ts"]) + planned
        try:
            self.storage.finish_pomodoro_session(
                self._session["id"], end_ts, planned, 0, 0, 0, "completed")
        except Exception:
            pass
        self._state = "idle"
        self._session = None
        self._chain_next = False

    def _abort_locked(self, reason):
        if not self._session:
            self._state = "idle"
            return
        # focus_done 里的会话已经 completed，不能再改成 aborted
        if self._session.get("status") != "running":
            self._state = "idle"
            self._session = None
            self._done_since_mono = None
            self._last_state = "idle"
            return
        kind = self._session["kind"]
        start_ts = int(self._session["started_ts"])
        elapsed = min(self._elapsed_ms_locked(),
                      int(self._session["planned_ms"]))
        end_ts = self.wall_ms()
        report = {}
        if kind == "focus" and elapsed >= 60000:
            report = self._build_report(start_ts, end_ts, elapsed)
        try:
            self.storage.finish_pomodoro_session(
                self._session["id"], end_ts, elapsed,
                self._paused_total_locked(), self._interruptions,
                self._afk_ms, "aborted",
                json.dumps(report, ensure_ascii=False) if report else "")
        except Exception:
            pass
        self._state = "idle"
        self._session = None
        self._done_since_mono = None
        self._last_state = "idle"

    def _build_report(self, start_ts, end_ts, focused_ms):
        """用采集数据算这段时间的真实分布（核心差异化）。"""
        out = {"start_ts": start_ts, "end_ts": end_ts,
               "focused_ms": int(focused_ms), "rate": None,
               "by_cat": [], "top_apps": [], "top_content": [],
               "work_ms": 0, "fun_ms": 0, "total_ms": 0, "text": ""}
        try:
            from .classifier import ActivityService
            svc = ActivityService(self.storage)
            by_app, by_cat, by_content = svc.durations(start_ts, end_ts)
        except Exception as e:
            print("[pomodoro] 生成报告失败: %s" % e)
            return out

        total = sum(by_app.values())
        work = by_cat.get("工作", 0)
        fun = by_cat.get("娱乐", 0)
        out["total_ms"] = total
        out["work_ms"] = work
        out["fun_ms"] = fun
        # 采集覆盖不足一半时不评判专注率：刚启动/采集暂停时 total 会很小，
        # 直接算会得出误导性的 0%。
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
            out["text"] = "基本在状态，有 %s 分给了别的。" % _human(fun or 0)
        else:
            out["text"] = "这次注意力比较散，%s 在娱乐/浏览上。" % _human(
                max(0, total - work))
        return out

    def _recover(self):
        """启动时处理上次未结束的会话。"""
        row = self.storage.get_running_pomodoro()
        if not row:
            return
        now_wall = self.wall_ms()
        planned = int(row["planned_ms"])
        started = int(row["started_ts"])
        elapsed = max(0, now_wall - started)
        if elapsed <= planned + config.POMODORO_RESUME_GRACE_MS:
            self._session = row
            self._state = row["kind"]
            self._start_mono = self.clock() - elapsed / 1000.0
            self._accum_pause_ms = int(row["paused_ms"] or 0)
            self._interruptions = int(row["interruptions"] or 0)
            self._afk_ms = int(row["afk_ms"] or 0)
            self._grace_until_mono = (self.clock()
                                      + config.POMODORO_START_GRACE_S)
            self._last_state = self._state
            print("[pomodoro] 恢复未结束会话 #%d（%s）" % (row["id"], row["kind"]))
        else:
            end_ts = started + min(elapsed, planned)
            try:
                self.storage.finish_pomodoro_session(
                    row["id"], end_ts, min(elapsed, planned),
                    int(row["paused_ms"] or 0), int(row["interruptions"] or 0),
                    int(row["afk_ms"] or 0), "interrupted")
            except Exception:
                pass
            print("[pomodoro] 会话 #%d 超时，标记 interrupted" % row["id"])

    def _notify(self, title, body):
        try:
            self.notifier(title, body)
        except Exception:
            pass

    def _announce(self, title, body, kind=None):
        """气泡必发；kind 非空时额外拉起置顶提醒小窗。"""
        self._notify(title, body)
        if not kind or not self.popup_cb:
            return
        try:
            self.popup_cb(kind, title, body)
        except Exception as e:
            print("[pomodoro] 提醒窗拉起失败: %s" % e)

    def _emit_state(self):
        if not self.state_cb:
            return
        try:
            self.state_cb(self.status())
        except Exception:
            pass
