# -*- coding: utf-8 -*-
"""待办 REST API 集成测试（临时库 + 独立端口，不碰真实数据）。

运行：python probes/test_todo_api.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder import web
from screen_time_recorder.storage import Storage

PORT = 8767
BASE = "http://127.0.0.1:%d" % PORT


def req(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else {}


def main():
    st = Storage(os.path.join(tempfile.mkdtemp(), "api.db"))
    app = web.create_app(st)
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True).start()
    time.sleep(1.3)

    # 1) 创建
    status, body = req("/api/todos", "POST",
                       {"title": "写周报", "remind_ts": int(time.time() * 1000) + 60000,
                        "priority": 2})
    assert status == 201 and body["todo"]["title"] == "写周报", (status, body)
    tid = body["todo"]["id"]
    print("创建 OK id =", tid)

    # 2) 列表 + 摘要
    status, body = req("/api/todos?filter=today")
    assert status == 200 and len(body["todos"]) == 1, body
    status, body = req("/api/todos/summary")
    assert body["open"] == 1 and body["due_today"] == 1, body
    print("列表 / 摘要 OK", body)

    # 3) 修改
    status, body = req("/api/todos/%d" % tid, "PATCH",
                       {"title": "写周报（P0）", "note": "先写采集部分"})
    assert status == 200 and body["todo"]["note"] == "先写采集部分", body
    print("修改 OK")

    # 4) 稍后提醒
    status, body = req("/api/todos/%d/snooze" % tid, "POST", {"minutes": 10})
    assert status == 200, body
    assert st.get_todo(tid)["snooze_ts"] is not None
    print("稍后提醒 OK")

    # 5) 完成 / 取消完成
    status, body = req("/api/todos/%d/done" % tid, "POST", {"done": True})
    assert status == 200 and body["todo"]["status"] == "done", body
    status, body = req("/api/todos/%d/done" % tid, "POST", {"done": False})
    assert status == 200 and body["todo"]["status"] == "open", body
    print("完成 / 取消 OK")

    # 6) 参数校验
    status, body = req("/api/todos", "POST", {"title": "   "})
    assert status == 400, (status, body)
    status, body = req("/api/todos/999999", "PATCH", {"title": "x"})
    assert status == 404, (status, body)
    status, body = req("/api/todos/999999/done", "POST", {"done": True})
    assert status == 404, (status, body)
    print("参数校验 OK（400 / 404）")

    # 7) 删除
    status, body = req("/api/todos/%d" % tid, "DELETE")
    assert status == 200 and body["deleted"] == 1, body
    status, body = req("/api/todos?filter=all")
    assert body["todos"] == [], body
    print("删除 OK")

    st.close()
    print("all todo api tests passed")


if __name__ == "__main__":
    main()
