# -*- coding: utf-8 -*-
"""番茄钟 REST API 集成测试（临时库 + Flask test_client，不监听端口）。"""
import os
import sys
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from screen_time_recorder.storage import Storage
from screen_time_recorder.pomodoro import PomodoroService
from screen_time_recorder import web


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


def main():
    st = Storage(os.path.join(tempfile.mkdtemp(), "api.db"))
    clock, wall = FakeClock(), FakeWall()
    svc = PomodoroService(st, clock=clock, wall_ms=wall, tick_ms=1000)
    app = web.create_app(st, pomodoro=svc)
    c = app.test_client()

    r = c.get("/api/pomodoro")
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body["state"] == "idle"
    assert body["config"] == {"focus_minutes": 25, "break_minutes": 5}, body
    print("GET /api/pomodoro 初始状态 OK")

    r = c.put("/api/settings/pomodoro",
              json={"focus_minutes": 50, "break_minutes": 10})
    assert r.status_code == 200
    assert r.get_json() == {"focus_minutes": 50, "break_minutes": 10}
    r = c.put("/api/settings/pomodoro",
              json={"focus_minutes": 999, "break_minutes": -5})
    assert r.get_json() == {"focus_minutes": 180, "break_minutes": 0}
    print("GET/PUT /api/settings/pomodoro 夹紧 OK")

    r = c.post("/api/pomodoro/start",
               json={"focus_minutes": 50, "break_minutes": 10,
                     "title": "API 测试"})
    assert r.status_code == 200, r.data
    stt = r.get_json()["state"]
    assert stt["state"] == "focus" and stt["remaining_ms"] == 50 * 60000
    assert stt["title"] == "API 测试"
    print("POST /api/pomodoro/start OK")

    clock.advance(60)
    wall.advance(60)
    r = c.get("/api/pomodoro")
    assert r.get_json()["remaining_ms"] == 49 * 60000
    print("倒计时读取 OK")

    r = c.post("/api/pomodoro/pause", json={})
    assert r.get_json()["state"]["state"] == "paused"
    r = c.post("/api/pomodoro/resume", json={})
    assert r.get_json()["state"]["state"] == "focus"
    print("pause / resume OK")

    r = c.post("/api/pomodoro/stop", json={"reason": "test"})
    assert r.get_json()["state"]["state"] == "idle"
    rows = st.recent_pomodoro(1)
    assert rows and rows[0]["status"] == "aborted", rows
    sid = rows[0]["id"]
    print("stop → aborted OK")

    r = c.get("/api/pomodoro/report/%d" % sid)
    assert r.status_code == 200
    assert r.get_json()["session"]["id"] == sid
    r = c.get("/api/pomodoro/report/999999")
    assert r.status_code == 404
    print("report 查询 / 404 OK")

    r = c.post("/api/pomodoro/start",
               json={"focus_minutes": "abc"})
    assert r.status_code == 400, r.data
    print("非法参数 400 OK")

    # 无服务时返回 503
    app2 = web.create_app(st)
    c2 = app2.test_client()
    assert c2.get("/api/pomodoro").status_code == 503
    print("未启动服务 503 OK")

    st.close()
    print("all pomodoro api tests passed")


if __name__ == "__main__":
    main()
