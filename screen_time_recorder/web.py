# -*- coding: utf-8 -*-
"""本地仪表盘 Web 服务（仅监听 127.0.0.1）。

随采集器常驻启动；托盘菜单「打开仪表盘」直接打开。
调试可单独运行：python main.py dashboard
"""
import os
import threading
import webbrowser

from flask import Flask, jsonify, request, send_file, send_from_directory

from . import dashboard

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(BASE, "dashboard_live.html")
ASSETS_DIR = os.path.join(BASE, "assets")
DEFAULT_PORT = 8765


def create_app(storage):
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
        return jsonify(dashboard.app_detail(storage, date, app_name))

    @app.get("/api/edits")
    def api_edits():
        return jsonify({"edits": dashboard.list_edits(
            storage, request.args.get("date"))})

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
        try:
            tid = storage.add_todo(
                body.get("title"), body.get("note") or "",
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

    return app


def _as_ms(value):
    """毫秒时间戳容错：None/空/0 → None；数字或数字字符串 → int。"""
    if value in (None, "", 0):
        return None
    return int(value)


def start_server(storage, port=DEFAULT_PORT, open_browser=False):
    """后台线程启动服务，返回访问 URL。"""
    import logging
    # 关掉 werkzeug 的每条请求日志：否则会灌满 tray.log
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app = create_app(storage)
    url = f"http://127.0.0.1:{port}/"

    def run():
        app.run(host="127.0.0.1", port=port, debug=False,
                use_reloader=False, threaded=True)

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
