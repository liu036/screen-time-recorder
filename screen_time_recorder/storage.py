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
"""


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
