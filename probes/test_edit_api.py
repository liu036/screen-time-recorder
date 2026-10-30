# -*- coding: utf-8 -*-
"""仪表盘编辑 API 集成测试（临时库，不碰真实数据）。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.storage import Storage
from screen_time_recorder import web

TODAY = time.strftime("%Y-%m-%d")
BASE = int(time.mktime(time.strptime(TODAY, "%Y-%m-%d")) * 1000) + 9 * 3600 * 1000


def main():
    st = Storage(os.path.join(tempfile.mkdtemp(), "t.db"))
    fx = st.ensure_app("firefox.exe", None, "Mozilla Firefox", "MozillaWindowClass")
    st._conn.executemany(
        "INSERT INTO events(ts,event_type,hwnd,app_id,window_title,rect,cls,"
        "idle_ms,is_afk,raw,content_title) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(BASE, "foreground", 1, fx, "页面一 — Mozilla Firefox", "", "", 0, 0, "", None),
         (BASE + 120000, "foreground", 1, fx, "页面二 — Mozilla Firefox", "", "", 0, 0, "", None),
         (BASE + 240000, "foreground", 1, fx, "页面三 — Mozilla Firefox", "", "", 0, 0, "", None)])
    st._conn.commit()

    app = web.create_app(st)
    c = app.test_client()

    # 1) 日数据：时间线应带 st/en/ed
    r = c.get(f"/api/day?date={TODAY}")
    assert r.status_code == 200, r.status_code
    d = r.get_json()["data"][TODAY]
    print("segments:", len(d["timeline"]), "| edited:", d["edited"],
          "| cat_options:", d["cat_options"])
    assert len(d["timeline"]) == 3
    assert {"st", "en", "ed"} <= set(d["timeline"][0])
    first = d["timeline"][0]

    # 2) 改分类（单段）
    r = c.post("/api/edit", json={"op": "set_category", "app": "Mozilla Firefox",
                                  "start": first["st"], "category": "工作",
                                  "scope": "segment"})
    assert r.status_code == 200, r.get_data(as_text=True)
    eid = r.get_json()["id"]
    d = c.get(f"/api/day?date={TODAY}").get_json()["data"][TODAY]
    seg0 = [x for x in d["timeline"] if x["st"] == first["st"]][0]
    assert seg0["cat"] == "工作", seg0["cat"]
    assert seg0["ed"] is True
    assert d["edited"] == 1
    print("单段改分类 OK ->", seg0["cat"])

    # 3) 撤销
    assert c.post("/api/edit", json={"op": "undo", "id": eid}).status_code == 200
    d = c.get(f"/api/day?date={TODAY}").get_json()["data"][TODAY]
    assert d["edited"] == 0
    print("撤销 OK")

    # 4) 应用级改分类
    c.post("/api/edit", json={"op": "set_category", "app": "Mozilla Firefox",
                              "start": first["st"], "category": "娱乐", "scope": "app"})
    d = c.get(f"/api/day?date={TODAY}").get_json()["data"][TODAY]
    assert all(x["cat"] == "娱乐" for x in d["timeline"])
    print("应用级改分类 OK，by_cat =", d["by_cat"])

    # 5) 删除某段
    before = d["total_ms"]
    r = c.post("/api/edit", json={"op": "exclude", "app": "Mozilla Firefox",
                                  "start": first["st"], "end": first["en"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = c.get(f"/api/day?date={TODAY}").get_json()["data"][TODAY]
    assert abs((before - d["total_ms"]) - 120000) < 2000, (before, d["total_ms"])
    print("删除段 OK ->", before, "→", d["total_ms"])

    # 6) 编辑记录可列出
    edits = c.get("/api/edits").get_json()["edits"]
    print("edits:", [(e["id"], e["type"], e["action"]) for e in edits])
    assert len(edits) == 2

    # 7) 坏参数不 500
    assert c.post("/api/edit", json={"op": "exclude", "app": "x",
                                     "start": 0, "end": 0}).status_code == 400
    assert c.post("/api/edit", json={"op": "nope"}).status_code == 400
    print("参数校验 OK")

    st.close()
    print("ALL EDIT API TESTS PASSED")


if __name__ == "__main__":
    main()
