# -*- coding: utf-8 -*-
"""SQLite 存储层（MVP）。

设计要点（来自《3-技术调研报告》T3 容量结论）：
- events 表只存 app_id（引用 apps 表），不重复存 exe_path/app_name → 单条 ~150B 而非 ~267B
- apps 表做窗口元数据去重（进程名 → 应用实体）
- visibility_snapshot 抽屉式：1s 检测、布局变化才写，且只存 (hwnd, rect, visibility, cover, app_id)
- 写操作走内存缓冲 + 1s 定时 flush，降低 IO 次数
- edits 表实现用户数据最高权限（Override 层）
"""
import json
import sqlite3
import threading
import time

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    app_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    process_name TEXT NOT NULL,
    exe_path     TEXT,
    app_name     TEXT,
    cls          TEXT,
    first_seen   INTEGER,
    UNIQUE(process_name)
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,            -- UTC 毫秒
    event_type   TEXT NOT NULL,               -- foreground/title_change/afk/heartbeat/lock/unlock
    hwnd         INTEGER,
    app_id       INTEGER,                     -- 关联 apps（容量优化：不重复存元数据）
    window_title TEXT,                        -- 隐私敏感；IM 类留空
    rect         TEXT,                        -- "l,t,w,h"
    cls          TEXT,
    idle_ms      INTEGER,
    is_afk       INTEGER,
    raw          TEXT,                        -- JSON 兜底扩展字段
    content_title TEXT                        -- 内容级标题（如抖音正在播放的视频/作者）；应用级为 NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS visibility_snapshot (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    hwnd       INTEGER,
    app_id     INTEGER,
    rect       TEXT,
    visibility TEXT,                          -- focus/visible/hidden
    cover      REAL                           -- 屏幕覆盖率（clamp 后）
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON visibility_snapshot(ts);

CREATE TABLE IF NOT EXISTS category_rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    match_field  TEXT,
    pattern      TEXT,
    category     TEXT,
    sub_category TEXT,
    productivity TEXT,
    priority     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    target_type TEXT,                         -- activity/event/day
    target_id   TEXT,
    action      TEXT,                         -- modify/merge/split/delete
    payload     TEXT,
    applied     INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS todos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    note        TEXT    DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'open',   -- open/done/archived
    priority    INTEGER DEFAULT 1,                 -- 0低 1中 2高
    due_ts      INTEGER,                           -- 截止时间(ms)，NULL=无期限
    remind_ts   INTEGER,                           -- 提醒时间(ms)，NULL=不提醒
    reminded_ts INTEGER,                           -- 已提醒时间(ms)，用于去重
    snooze_ts   INTEGER,                           -- 稍后提醒到(ms)
    repeat_rule TEXT,                              -- 预留：daily/weekly/...
    sort_order  INTEGER DEFAULT 0,
    created_ts  INTEGER NOT NULL,
    done_ts     INTEGER,
    source      TEXT    DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status, due_ts);
CREATE INDEX IF NOT EXISTS idx_todos_remind ON todos(status, remind_ts);
"""

_TODO_COLS = ("id", "title", "note", "status", "priority", "due_ts",
              "remind_ts", "reminded_ts", "snooze_ts", "repeat_rule",
              "sort_order", "created_ts", "done_ts", "source")
_TODO_SELECT = ("SELECT id,title,note,status,priority,due_ts,remind_ts,"
                "reminded_ts,snooze_ts,repeat_rule,sort_order,created_ts,"
                "done_ts,source FROM todos")
# 允许通过 update_todo 修改的字段（白名单，防注入 / 防误改）
_TODO_UPDATABLE = {"title", "note", "status", "priority",
                   "due_ts", "remind_ts", "repeat_rule", "sort_order"}


def _local_day_start_ms(ts_ms):
    """某个时间戳所在本地日期的 00:00（毫秒）。"""
    t = time.localtime(ts_ms / 1000.0)
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday,
                            0, 0, 0, 0, 0, -1)) * 1000)


class Storage:
    def __init__(self, db_path=None):
        self.db_path = db_path or config.DB_PATH
        self._lock = threading.Lock()
        self._event_buf = []
        self._snap_buf = []
        self._app_cache = {}          # process_name -> app_id
        self._stop = threading.Event()
        self._conn = self._connect()
        self._init_schema()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

    # ---------- 连接与建表 ----------
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        # WAL 到 ~1MB 就自动 checkpoint，避免长期堆积到 4MB 后在异常退出时丢尾巴
        conn.execute("PRAGMA wal_autocheckpoint=256")
        return conn

    def _init_schema(self):
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._migrate()
        self._seed_rules()

    def _migrate(self):
        """兼容旧库：为 events 增加 content_title 列（抖音等内容级记录用）。"""
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(events)").fetchall()}
        if "content_title" not in cols:
            with self._conn:  # 显式提交，避免依赖后续写操作顺带 commit
                self._conn.execute("ALTER TABLE events ADD COLUMN content_title TEXT")

    def _seed_rules(self):
        with self._conn:
            n = self._conn.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0]
            if n == 0:
                self._conn.executemany(
                    "INSERT INTO category_rules "
                    "(match_field,pattern,category,sub_category,productivity,priority) "
                    "VALUES (?,?,?,?,?,?)",
                    config.DEFAULT_CATEGORY_RULES,
                )

    # ---------- 应用归一化（apps 去重）----------
    def ensure_app(self, process_name, exe_path=None, app_name=None, cls=None):
        if not process_name:
            process_name = "unknown"
        cached = self._app_cache.get(process_name)
        if cached is not None:
            return cached
        with self._lock:
            row = self._conn.execute(
                "SELECT app_id FROM apps WHERE process_name=?", (process_name,)
            ).fetchone()
            if row:
                self._app_cache[process_name] = row[0]
                return row[0]
            self._conn.execute(
                "INSERT OR IGNORE INTO apps "
                "(process_name,exe_path,app_name,cls,first_seen) VALUES (?,?,?,?,?)",
                (process_name, exe_path, app_name, cls, int(time.time() * 1000)),
            )
            aid = self._conn.execute(
                "SELECT app_id FROM apps WHERE process_name=?", (process_name,)
            ).fetchone()[0]
            self._app_cache[process_name] = aid
            return aid

    # ---------- 写入（缓冲）----------
    def buffer_event(self, **kw):
        if getattr(self, "_closed", False):
            return
        kw.setdefault("content_title", None)
        with self._lock:
            self._event_buf.append(kw)

    def buffer_snapshot(self, **kw):
        if getattr(self, "_closed", False):
            return
        with self._lock:
            self._snap_buf.append(kw)

    def _flush_loop(self):
        while not self._stop.is_set():
            time.sleep(1.0)
            self._flush()

    def _flush(self):
        with self._lock:
            evs = self._event_buf
            self._event_buf = []
            snaps = self._snap_buf
            self._snap_buf = []
        if not evs and not snaps:
            return
        try:
            with self._conn:
                if evs:
                    self._conn.executemany(
                        "INSERT INTO events "
                        "(ts,event_type,hwnd,app_id,window_title,rect,cls,idle_ms,is_afk,raw,content_title) "
                        "VALUES (:ts,:event_type,:hwnd,:app_id,:window_title,:rect,:cls,"
                        ":idle_ms,:is_afk,:raw,:content_title)",
                        evs,
                    )
                if snaps:
                    self._conn.executemany(
                        "INSERT INTO visibility_snapshot "
                        "(ts,hwnd,app_id,rect,visibility,cover) "
                        "VALUES (:ts,:hwnd,:app_id,:rect,:visibility,:cover)",
                        snaps,
                    )
        except Exception as e:  # 写失败放回缓冲，下次重试
            with self._lock:
                self._event_buf = evs + self._event_buf
                self._snap_buf = snaps + self._snap_buf
            print(f"[storage] flush error: {e}")

    # ---------- 查询（供 classifier / CLI 使用）----------
    def get_app(self, app_id):
        with self._lock:
            return self._conn.execute(
                "SELECT app_id,process_name,exe_path,app_name,cls FROM apps WHERE app_id=?",
                (app_id,),
            ).fetchone()

    def count_events(self):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def count_snapshots(self):
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM visibility_snapshot").fetchone()[0]

    def iter_events(self, from_ts=0, limit=5000000):
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,event_type,hwnd,app_id,window_title,rect,cls,idle_ms,is_afk,content_title "
                "FROM events WHERE ts>=? ORDER BY ts LIMIT ?",
                (from_ts, limit),
            ).fetchall()
        return rows

    def iter_events_between(self, from_ts, to_ts, limit=5000000):
        """按区间查询（含起始、不含结束）。避免查历史日期时把之后的事件全加载进来。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,event_type,hwnd,app_id,window_title,rect,cls,idle_ms,is_afk,content_title "
                "FROM events WHERE ts>=? AND ts<? ORDER BY ts LIMIT ?",
                (from_ts, to_ts, limit),
            ).fetchall()
        return rows

    def get_all_apps(self):
        with self._lock:
            return self._conn.execute(
                "SELECT app_id,process_name,app_name,cls FROM apps ORDER BY app_id"
            ).fetchall()

    def get_rules(self):
        with self._lock:
            return self._conn.execute(
                "SELECT id,match_field,pattern,category,sub_category,productivity,priority "
                "FROM category_rules ORDER BY priority DESC"
            ).fetchall()

    def ts_range(self):
        """事件的最小/最大时间戳（ms），无数据时返回 (None, None)。"""
        with self._lock:
            return self._conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM events").fetchone()

    # ---------- 待办 ----------
    def add_todo(self, title, note="", due_ts=None, remind_ts=None, priority=1):
        """新增待办，返回 id。标题为空抛 ValueError。"""
        title = (title or "").strip()
        if not title:
            raise ValueError("待办标题不能为空")
        priority = max(0, min(2, int(priority if priority is not None else 1)))
        now = int(time.time() * 1000)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO todos (title,note,status,priority,due_ts,remind_ts,"
                "sort_order,created_ts,source) "
                "VALUES (?,?,'open',?,?,?,0,?,'manual')",
                (title, note or "", priority, due_ts, remind_ts, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_todo(self, todo_id):
        with self._lock:
            row = self._conn.execute(
                _TODO_SELECT + " WHERE id=?", (int(todo_id),)).fetchone()
        return dict(zip(_TODO_COLS, row)) if row else None

    def list_todos(self, filter="open", now_ms=None, limit=200):
        """filter: open / today / all。

        today = 未完成全部 + 今天完成的（这样下班前能看到当天战果）；
        all   = 除归档外全部。排序：未完成优先 → 逾期优先 → 临近的优先。
        """
        now = int(now_ms or time.time() * 1000)
        params = []
        if filter == "open":
            where = "status='open'"
        elif filter == "today":
            where = "status='open' OR (status='done' AND done_ts>=?)"
            params.append(_local_day_start_ms(now))
        else:  # all
            where = "status<>'archived'"
        sql = (_TODO_SELECT + " WHERE " + where + " ORDER BY "
               "CASE status WHEN 'open' THEN 0 ELSE 1 END, "
               "CASE "
               "  WHEN status='open' AND due_ts IS NOT NULL AND due_ts<? THEN 0 "
               "  WHEN status='open' AND (due_ts IS NOT NULL OR remind_ts IS NOT NULL) THEN 1 "
               "  WHEN status='open' THEN 2 ELSE 3 END, "
               "COALESCE(due_ts, remind_ts, 9999999999999), priority DESC, id DESC "
               "LIMIT ?")
        with self._lock:
            rows = self._conn.execute(
                sql, tuple(params) + (now, int(limit))).fetchall()
        return [dict(zip(_TODO_COLS, r)) for r in rows]

    def update_todo(self, todo_id, **fields):
        """更新待办字段（白名单）。返回是否命中。"""
        unknown = set(fields) - _TODO_UPDATABLE
        if unknown:
            raise ValueError("不支持的字段: " + ", ".join(sorted(unknown)))
        if "title" in fields:
            fields["title"] = (fields["title"] or "").strip()
            if not fields["title"]:
                raise ValueError("待办标题不能为空")
        if "priority" in fields:
            fields["priority"] = max(0, min(2, int(fields["priority"])))

        sets, params = [], []
        for k, v in fields.items():
            sets.append(k + "=?")
            params.append(v)
        # 改了提醒时间 → 允许重新提醒（清掉已提醒与稍后状态）
        if "remind_ts" in fields:
            sets.extend(["reminded_ts=NULL", "snooze_ts=NULL"])
        # 状态切换 → 维护 done_ts
        if fields.get("status") == "done":
            sets.append("done_ts=?")
            params.append(int(time.time() * 1000))
        elif fields.get("status") == "open":
            sets.append("done_ts=NULL")
        if not sets:
            return False
        params.append(int(todo_id))
        with self._lock:
            n = self._conn.execute(
                "UPDATE todos SET " + ", ".join(sets) + " WHERE id=?",
                params).rowcount
            self._conn.commit()
        return n > 0

    def complete_todo(self, todo_id, done=True):
        return self.update_todo(todo_id, status="done" if done else "open")

    def delete_todo(self, todo_id):
        with self._lock:
            n = self._conn.execute(
                "DELETE FROM todos WHERE id=?", (int(todo_id),)).rowcount
            self._conn.commit()
        return n

    def due_todos(self, now_ms=None):
        """到点待提醒的待办（snooze 到期也在这里命中）。"""
        now = int(now_ms or time.time() * 1000)
        with self._lock:
            rows = self._conn.execute(
                _TODO_SELECT + " WHERE status='open' AND remind_ts IS NOT NULL "
                "AND remind_ts<=? "
                "AND (reminded_ts IS NULL OR reminded_ts<remind_ts) "
                "AND (snooze_ts IS NULL OR snooze_ts<=?) "
                "ORDER BY remind_ts, priority DESC",
                (now, now)).fetchall()
        return [dict(zip(_TODO_COLS, r)) for r in rows]

    def mark_reminded(self, todo_id, ts_ms=None):
        """标记已提醒。必须先写库再弹通知，避免异常重启后重复轰炸。"""
        ts = int(ts_ms or time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "UPDATE todos SET reminded_ts=? WHERE id=?",
                (ts, int(todo_id)))
            self._conn.commit()

    def snooze_todo(self, todo_id, minutes=10, now_ms=None):
        """稍后提醒：清空 reminded_ts 让 snooze 到期后能再次命中。"""
        now = int(now_ms or time.time() * 1000)
        until = now + max(1, int(minutes)) * 60000
        with self._lock:
            n = self._conn.execute(
                "UPDATE todos SET snooze_ts=?, reminded_ts=NULL WHERE id=?",
                (until, int(todo_id))).rowcount
            self._conn.commit()
        return n > 0

    def todo_summary(self, now_ms=None):
        now = int(now_ms or time.time() * 1000)
        day0 = _local_day_start_ms(now)
        day1 = day0 + 86400000
        with self._lock:
            row = self._conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN status='open' AND due_ts IS NOT NULL "
                "  AND due_ts<? THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN status='open' AND "
                "  ((due_ts>=? AND due_ts<?) OR (remind_ts>=? AND remind_ts<?)) "
                "  THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN status='done' AND done_ts>=? "
                "  THEN 1 ELSE 0 END),0) "
                "FROM todos",
                (now, day0, day1, day0, day1, day0)).fetchone()
        return {"open": row[0], "overdue": row[1],
                "due_today": row[2], "done_today": row[3]}

    # ---------- 用户编辑层（Override，用户数据最高权限）----------
    def add_edit(self, target_type, target_id, action, payload):
        """写入一条编辑记录，返回 edit id。

        target_type: app / segment / range
        action:      set_category / exclude
        payload:     dict（JSON 存储）
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO edits (ts,target_type,target_id,action,payload,applied) "
                "VALUES (?,?,?,?,?,1)",
                (int(time.time() * 1000), target_type, target_id or "",
                 action, json.dumps(payload or {}, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_edits(self, applied_only=True):
        sql = ("SELECT id,ts,target_type,target_id,action,payload,applied "
               "FROM edits")
        if applied_only:
            sql += " WHERE applied=1"
        sql += " ORDER BY id"
        with self._lock:
            return self._conn.execute(sql).fetchall()

    def delete_edit(self, edit_id):
        """撤销一条编辑（删除记录，派生即恢复原状）。"""
        with self._lock:
            n = self._conn.execute(
                "DELETE FROM edits WHERE id=?", (int(edit_id),)).rowcount
            self._conn.commit()
        return n

    def delete_range(self, from_ts, to_ts):
        """删除区间内的原始事件与快照（含起始、不含结束），返回条数。"""
        with self._lock:
            n_e = self._conn.execute(
                "DELETE FROM events WHERE ts>=? AND ts<?",
                (from_ts, to_ts)).rowcount
            n_s = self._conn.execute(
                "DELETE FROM visibility_snapshot WHERE ts>=? AND ts<?",
                (from_ts, to_ts)).rowcount
            self._conn.commit()
        return n_e, n_s

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self._stop.set()
        self._flush()
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
