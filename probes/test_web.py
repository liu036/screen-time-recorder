# -*- coding: utf-8 -*-
"""验证本地仪表盘 Web 服务：API 与页面。"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.storage import Storage
from screen_time_recorder.web import start_server

PORT = 8766  # 避开采集器正式端口


def get(url):
    r = urllib.request.urlopen(url, timeout=90)
    return r.status, r.read()


def main():
    st = Storage()
    url = start_server(st, PORT, open_browser=False)
    time.sleep(2)
    base = f"http://127.0.0.1:{PORT}"

    status, body = get(base + "/api/days")
    days = json.loads(body)["days"]
    print("GET /api/days ->", status, "days:", days[:5], "count", len(days))
    assert days, "no days"

    day = days[0]
    status, body = get(base + "/api/day?date=" + day)
    d = json.loads(body)
    p = d["data"][d["days"][0]]
    print(f"GET /api/day?date={day} ->", status,
          "total", p["total"], "apps", len(p["by_app"]),
          "timeline", len(p["timeline"]), "content", len(p["content"]),
          "week_rows", len(d["week_hourly"]))
    assert p["total_ms"] >= 0

    start = d["week_start"]
    status, body = get(base + "/api/week?start=" + start)
    w = json.loads(body)
    wp = w["data"]["__week__"]
    print(f"GET /api/week?start={start} ->", status,
          "total", wp["total"], "apps", len(wp["by_app"]),
          "hourly_rows", len(w["week_hourly"]))
    assert len(w["week_hourly"]) == 7

    app_name = p["by_app"][0][0]
    status, body = get(base + "/api/app?date=" + day
                       + "&app=" + urllib.parse.quote(app_name))
    a = json.loads(body)
    print(f"GET /api/app ({app_name}) ->", status,
          "items", len(a["items"]), "total_ms", a["total_ms"])
    assert a["items"]

    status, body = get(base + "/")
    print("GET / ->", status, "html bytes", len(body))
    assert b"<html" in body.lower()

    st.close()
    print("\nWeb 服务全部通过 ✅")


if __name__ == "__main__":
    main()
