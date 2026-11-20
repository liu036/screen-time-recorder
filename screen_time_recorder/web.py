# -*- coding: utf-8 -*-
"""本地仪表盘 Web 服务（仅监听 127.0.0.1）。

随采集器常驻启动；托盘菜单「打开仪表盘」直接打开。
调试可单独运行：python main.py dashboard
"""
import json
import os
import threading
import webbrowser
from datetime import datetime

from flask import Flask, jsonify, request, send_file, send_from_directory

from . import dashboard

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(BASE, "dashboard_live.html")
ASSETS_DIR = os.path.join(BASE, "assets")
DEFAULT_PORT = 8765


def _port_listening(port, timeout=0.3):
    """端口上是否已有服务在监听。

    Windows 的 SO_REUSEADDR 允许多个 socket 绑同一端口（不像 Unix 会失败），
    所以 make_server 不会抛错，而是静默抢不到连接。必须主动连一下。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False
    finally:
        s.close()


def _valid_date(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def create_app(storage, pomodoro=None):
    app = Flask(__name__)

    @app.get("/")
    def index():
        return send_file(HTML_PATH)

    @app.get("/assets/<path:filename>")
    def assets(filename):
        """本地静态资源（图表库内置，不依赖外网 CDN）。"""
        return send_from_directory(ASSETS_DIR, filename)

    @app.get("/api/days")
    def api_days():
        return jsonify({"days": dashboard.available_days(storage)})

    @app.get("/api/day")
    def api_day():
        date = request.args.get("date")
        if date and not _valid_date(date):
            return jsonify({"error": "date 格式应为 YYYY-MM-DD"}), 400
        if not date:
            days = dashboard.available_days(storage)
            if not days:
                return jsonify({"error": "no data"}), 404
            date = days[0]
        return jsonify({
            "days": [date],
            "data": {date: dashboard.day_payload(storage, date)},
            **dashboard.week_context(storage, date),
        })

    @app.get("/api/week")
    def api_week():
        start = request.args.get("start")
        if start and not _valid_date(start):
            return jsonify({"error": "start 格式应为 YYYY-MM-DD"}), 400
        if not start:
            days = dashboard.available_days(storage)
            if not days:
                return jsonify({"error": "no data"}), 404
            start = dashboard.week_context(storage, days[0])["week_start"]
        return jsonify(dashboard.week_payload(storage, start))

    @app.get("/api/app")
    def api_app():
        date = request.args.get("date")
        app_name = request.args.get("app")
        if not date or not app_name:
            return jsonify({"error": "date and app are required"}), 400
        if not _valid_date(date):
            return jsonify({"error": "date 格式应为 YYYY-MM-DD"}), 400
        return jsonify(dashboard.app_detail(storage, date, app_name))

    @app.get("/api/edits")
    def api_edits():
        date = request.args.get("date")
        if date and not _valid_date(date):
            return jsonify({"error": "date 格式应为 YYYY-MM-DD"}), 400
        return jsonify({"edits": dashboard.list_edits(storage, date)})

    @app.post("/api/edit")
    def api_edit():
        body = request.get_json(silent=True) or {}
        op = body.get("op")
        try:
            if op == "set_category":
                eid = dashboard.set_category(
                    storage, body.get("app"), body.get("start"),
                    body.get("category") or "未分类",
                    scope=body.get("scope") or "segment")
                return jsonify({"ok": True, "id": eid})
            if op == "exclude":
                eid = dashboard.exclude_segment(
                    storage, body.get("app"), body.get("start"), body.get("end"))
                return jsonify({"ok": True, "id": eid})
            if op == "undo":
                n = storage.delete_edit(body.get("id"))
                return jsonify({"ok": True, "undone": n})
            return jsonify({"error": "unknown op"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    # ---- 待办（待办窗口 / 托盘 / 仪表盘共用）----
    @app.get("/api/todos")
    def api_todos():
        f = request.args.get("filter", "open")
        if f not in ("today", "open", "all"):
            f = "open"
        return jsonify({"todos": storage.list_todos(f)})

    @app.get("/api/todos/summary")
    def api_todos_summary():
        return jsonify(storage.todo_summary())

    @app.post("/api/todos")
    def api_todo_add():
        body = request.get_json(silent=True) or {}
        title = (body.get("title") or "").strip()
        if not title:
            return jsonify({"error": "标题不能为空"}), 400
        try:
            tid = storage.add_todo(
                title, body.get("note") or "",
                _as_ms(body.get("due_ts")), _as_ms(body.get("remind_ts")),
                body.get("priority", 1))
        except (ValueError, TypeError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "todo": storage.get_todo(tid)}), 201

    @app.patch("/api/todos/<int:todo_id>")
    def api_todo_update(todo_id):
        body = request.get_json(silent=True) or {}
        fields = {}
        for k in ("title", "note", "status", "priority", "repeat_rule"):
            if k in body:
                fields[k] = body[k]
        for k in ("due_ts", "remind_ts"):
            if k in body:
                fields[k] = _as_ms(body[k])
        if not fields:
            todo = storage.get_todo(todo_id)
            if not todo:
                return jsonify({"error": "待办不存在"}), 404
            return jsonify({"ok": True, "todo": todo})
        try:
            ok = storage.update_todo(todo_id, **fields)
        except (ValueError, TypeError) as e:
            return jsonify({"error": str(e)}), 400
        if not ok:
            return jsonify({"error": "待办不存在"}), 404
        return jsonify({"ok": True, "todo": storage.get_todo(todo_id)})

    @app.post("/api/todos/<int:todo_id>/done")
    def api_todo_done(todo_id):
        body = request.get_json(silent=True) or {}
        if not storage.complete_todo(todo_id, bool(body.get("done", True))):
            return jsonify({"error": "待办不存在"}), 404
        return jsonify({"ok": True, "todo": storage.get_todo(todo_id)})

    @app.post("/api/todos/<int:todo_id>/snooze")
    def api_todo_snooze(todo_id):
        body = request.get_json(silent=True) or {}
        try:
            minutes = int(body.get("minutes", 10))
        except (TypeError, ValueError):
            return jsonify({"error": "minutes 必须是整数"}), 400
        if not storage.snooze_todo(todo_id, minutes):
            return jsonify({"error": "待办不存在"}), 404
        return jsonify({"ok": True})

    @app.delete("/api/todos/<int:todo_id>")
    def api_todo_delete(todo_id):
        return jsonify({"ok": True, "deleted": storage.delete_todo(todo_id)})

    # ---- 番茄钟（主进程持有唯一会话，窗口/托盘只发指令）----
    def _no_service():
        return jsonify({"error": "pomodoro service not running"}), 503

    def _int_or_none(value):
        if value in (None, ""):
            return None
        return int(value)

    @app.get("/api/pomodoro")
    def api_pomodoro():
        if pomodoro is None:
            return _no_service()
        return jsonify(pomodoro.status())

    @app.post("/api/pomodoro/start")
    def api_pomodoro_start():
        if pomodoro is None:
            return _no_service()
        body = request.get_json(silent=True) or {}
        try:
            fm = _int_or_none(body.get("focus_minutes"))
            bm = _int_or_none(body.get("break_minutes"))
            tid = _int_or_none(body.get("todo_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "时长与待办 id 必须是整数"}), 400
        return jsonify({"ok": True, "state": pomodoro.start_focus(
            focus_minutes=fm, break_minutes=bm, todo_id=tid,
            title=body.get("title"))})

    @app.post("/api/pomodoro/pause")
    def api_pomodoro_pause():
        if pomodoro is None:
            return _no_service()
        body = request.get_json(silent=True) or {}
        return jsonify({"ok": True, "state": pomodoro.pause(
            body.get("reason") or "manual")})

    @app.post("/api/pomodoro/resume")
    def api_pomodoro_resume():
        if pomodoro is None:
            return _no_service()
        return jsonify({"ok": True, "state": pomodoro.resume()})

    @app.post("/api/pomodoro/stop")
    def api_pomodoro_stop():
        if pomodoro is None:
            return _no_service()
        body = request.get_json(silent=True) or {}
        return jsonify({"ok": True, "state": pomodoro.stop(
            body.get("reason") or "manual")})

    @app.post("/api/pomodoro/start-break")
    def api_pomodoro_start_break():
        if pomodoro is None:
            return _no_service()
        body = request.get_json(silent=True) or {}
        return jsonify({"ok": True, "state": pomodoro.start_break(
            chain=bool(body.get("chain")))})

    @app.post("/api/pomodoro/next")
    def api_pomodoro_next():
        """跳过休息，沿用上一轮参数直接开始下一轮。"""
        if pomodoro is None:
            return _no_service()
        return jsonify({"ok": True, "state": pomodoro.next_round()})

    @app.post("/api/pomodoro/skip-break")
    def api_pomodoro_skip_break():
        if pomodoro is None:
            return _no_service()
        return jsonify({"ok": True, "state": pomodoro.skip_break()})

    @app.post("/api/pomodoro/ack-setup")
    def api_pomodoro_ack_setup():
        if pomodoro is not None:
            pomodoro.ack_setup()
        return jsonify({"ok": True})

    @app.get("/api/pomodoro/report/<int:session_id>")
    def api_pomodoro_report(session_id):
        row = storage.get_pomodoro_session(session_id)
        if not row:
            return jsonify({"error": "会话不存在"}), 404
        try:
            report = json.loads(row.get("report_json") or "{}")
        except (TypeError, ValueError):
            report = {}
        session = {k: v for k, v in row.items() if k != "report_json"}
        return jsonify({"session": session, "report": report})

    @app.get("/api/settings/pomodoro")
    def api_pomodoro_settings():
        return jsonify(storage.pomodoro_config())

    @app.put("/api/settings/pomodoro")
    def api_pomodoro_settings_put():
        body = request.get_json(silent=True) or {}
        try:
            # 服务在跑时由它统一落库并同步内存默认值，避免「改了设置但下次
            # 开始仍用旧时长」；服务没起（CLI 场景）就直接写库。
            if pomodoro is not None:
                cfg = pomodoro.set_config(body.get("focus_minutes"),
                                          body.get("break_minutes"),
                                          body.get("sound"))
            else:
                cfg = storage.set_pomodoro_config(
                    body.get("focus_minutes"), body.get("break_minutes"),
                    body.get("sound"))
        except (TypeError, ValueError):
            return jsonify({"error": "时长必须是整数"}), 400
        return jsonify(cfg)

    return app


def _as_ms(value):
    """毫秒时间戳容错：None/空/0 → None；数字或数字字符串 → int。"""
    if value in (None, "", 0):
        return None
    return int(value)


def start_server(storage, port=DEFAULT_PORT, open_browser=False, pomodoro=None):
    """后台线程启动服务，返回访问 URL。

    绑定放在调用线程里同步完成：端口被占用时立刻抛 OSError。
    之前用 app.run() 在子线程里 bind，失败只会在后台线程里静默退出，
    表现为托盘一切正常、但仪表盘和待办窗口全部连不上（极难排查）。
    """
    import logging
    from werkzeug.serving import make_server

    # 关掉 werkzeug 的每条请求日志：否则会灌满 tray.log
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app = create_app(storage, pomodoro=pomodoro)
    url = f"http://127.0.0.1:{port}/"

    if _port_listening(port):
        raise OSError(
            "127.0.0.1:%d 已被占用（可能残留了旧的采集器进程）" % port)
    server = make_server("127.0.0.1", port, app, threaded=True)

    def run():
        try:
            server.serve_forever()
        finally:
            server.server_close()

    threading.Thread(target=run, daemon=True, name="dashboard-web").start()
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    return url


def run_standalone(port=DEFAULT_PORT):
    """独立运行（调试用）。"""
    from .storage import Storage
    st = Storage()
    print(f"仪表盘: http://127.0.0.1:{port}/  (Ctrl+C 退出)")
    start_server(st, port, open_browser=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        st.close()
