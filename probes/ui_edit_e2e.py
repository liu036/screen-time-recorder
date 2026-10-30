# -*- coding: utf-8 -*-
"""端到端 UI 测试：改分类 / 删除段落 / 撤销（临时库 + 独立端口，不碰真实数据）。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import requests
import websocket

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen_time_recorder.storage import Storage
from screen_time_recorder import web

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 8766
DBG = 9336
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOT_DIR = os.path.join(BASE_DIR, "probes")
PROFILE = os.path.join(SHOT_DIR, "_cdp_profile4")
TODAY = time.strftime("%Y-%m-%d")


class CDP:
    def __init__(self, ws):
        self.ws, self.i, self.events = ws, 0, []

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if "method" in m:
                self.events.append(m)
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def errors(self):
        out = []
        for e in self.events:
            if e.get("method") == "Runtime.exceptionThrown":
                d = e["params"].get("exceptionDetails", {})
                desc = (d.get("exception") or {}).get("description", "")
                out.append(f"{d.get('text')} {desc}"[:300])
            elif e.get("method") == "Log.entryAdded":
                p = e["params"].get("entry", {})
                if p.get("level") in ("error", "warning"):
                    out.append(f"[{p.get('level')}] {p.get('text')}"[:300])
        return out

    def eval(self, expr, await_promise=False):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise)
        if "exceptionDetails" in r:
            raise RuntimeError(str(r["exceptionDetails"])[:400])
        return r.get("result", {}).get("value")

    def mouse(self, typ, x, y, buttons=0, click_count=0):
        self.send("Input.dispatchMouseEvent", type=typ, x=x, y=y,
                  button="left" if buttons or typ != "mouseMoved" else "none",
                  buttons=buttons, clickCount=click_count)

    def shot(self, path):
        import base64
        r = self.send("Page.captureScreenshot", format="png")
        open(path, "wb").write(base64.b64decode(r["data"]))


def seed():
    st = Storage(os.path.join(tempfile.mkdtemp(), "ui.db"))
    fx = st.ensure_app("firefox.exe", None, "Mozilla Firefox", "MozillaWindowClass")
    now = int(time.time() * 1000)
    t0 = now - 4 * 3600 * 1000
    # 只用浏览器数据：初始分类只有「浏览网页」，改分类后才会出现「娱乐」
    rows = [
        (t0, "foreground", 1, fx, "第一个页面 — Mozilla Firefox", "", "", 0, 0, "", None),
        (t0 + 1800000, "foreground", 1, fx, "第二个页面 — Mozilla Firefox", "", "", 0, 0, "", None),
        (t0 + 3600000, "foreground", 1, fx, "第三个页面 — Mozilla Firefox", "", "", 0, 0, "", None),
        (t0 + 5400000, "foreground", 1, fx, "第四个页面 — Mozilla Firefox", "", "", 0, 0, "", None),
    ]
    st._conn.executemany(
        "INSERT INTO events(ts,event_type,hwnd,app_id,window_title,rect,cls,"
        "idle_ms,is_afk,raw,content_title) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    st._conn.commit()
    return st


def main():
    st = seed()
    app = web.create_app(st)
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=PORT,
                                            debug=False, use_reloader=False,
                                            threaded=True), daemon=True).start()
    time.sleep(1.5)
    assert requests.get(f"http://127.0.0.1:{PORT}/").status_code == 200

    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu",
                             "--force-prefers-reduced-motion",
                             "--no-first-run", "--no-default-browser-check",
                             "--disable-extensions",
                             f"--remote-debugging-port={DBG}",
                             "--remote-allow-origins=*",
                             f"--user-data-dir={PROFILE}",
                             "--window-size=1280,900", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(60):
            try:
                tabs = requests.get(f"http://127.0.0.1:{DBG}/json", timeout=2).json()
                for t in tabs:
                    if t.get("type") == "page":
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.4)
        ws = websocket.create_connection(ws_url, timeout=30)
        c = CDP(ws)
        c.send("Page.enable")
        c.send("Runtime.enable")
        c.send("Log.enable")
        c.send("Page.navigate", url=f"http://127.0.0.1:{PORT}/")
        ready = False
        for _ in range(90):
            if (c.eval("typeof Chart!=='undefined' && typeof echarts!=='undefined'")
                    and c.eval("document.querySelectorAll('#timeline rect').length")):
                ready = True
                break
            time.sleep(0.5)
        assert ready, "页面未就绪（可能是 CDN 加载超时）"
        print("段数:", c.eval("TLSEGS.length"))
        c.eval("document.getElementById('timelineCard').scrollIntoView({block:'center'})")
        c.eval("window.confirm=()=>true")
        time.sleep(0.4)

        def seg_point(idx):
            return c.eval(f"""(()=>{{
              const s=document.getElementById('timeline');
              const r=s.getBoundingClientRect(), vb=s.viewBox.baseVal;
              const sc=Math.min(r.width/vb.width, r.height/vb.height);
              const ox=r.left+(r.width-vb.width*sc)/2, oy=r.top+(r.height-vb.height*sc)/2;
              const span=TLV.hi-TLV.lo, seg=TLSEGS[{idx}];
              const vx=TLX0+(TLX1-TLX0)*((seg.sf+seg.ef)/2-TLV.lo)/span;
              return {{x:ox+vx*sc, y:oy+(TLY+TLH/2)*sc, app:seg.app, cat:seg.cat}};
            }})()""")

        def click(x, y):
            c.mouse("mouseMoved", x, y)
            c.mouse("mousePressed", x, y, buttons=1, click_count=1)
            c.mouse("mouseReleased", x, y, buttons=0, click_count=1)
            time.sleep(0.45)

        def wait_js(expr, timeout=10.0):
            end = time.time() + timeout
            while time.time() < end:
                try:
                    if c.eval(expr):
                        return True
                except Exception:
                    pass
                time.sleep(0.35)
            return False

        # ---- 1) 改分类（仅此段）----
        p = seg_point(0)
        print("目标段:", p)
        click(p["x"], p["y"])
        assert c.eval("document.getElementById('epop').classList.contains('show')")
        cats = c.eval("Array.from(document.querySelectorAll('#epChips button')).map(b=>b.textContent)")
        print("分类选项:", cats)
        c.eval("[...document.querySelectorAll('#epChips button')]"
               ".find(b=>b.textContent==='娱乐').click()")
        assert wait_js("DATA.data[CUR].by_cat.some(x=>x[0]==='娱乐')"), "改分类未生效"
        print("改分类后 by_cat:", c.eval("DATA.data[CUR].by_cat.map(x=>x[0])"))
        assert wait_js("document.getElementById('undoLast').classList.contains('on')"), \
            "撤销按钮未出现"
        print("撤销按钮文案:", c.eval("document.getElementById('undoLast').textContent"))
        assert len(requests.get(f"http://127.0.0.1:{PORT}/api/edits?date={TODAY}").json()["edits"]) == 1
        print("✅ 改分类（单段）生效，撤销按钮已亮")
        c.shot(os.path.join(SHOT_DIR, "_e2e_after_cat.png"))

        # ---- 2) 撤销 ----
        c.eval("document.getElementById('undoLast').click()")
        assert wait_js("!DATA.data[CUR].by_cat.some(x=>x[0]==='娱乐')"), "撤销未恢复分类"
        assert len(requests.get(f"http://127.0.0.1:{PORT}/api/edits?date={TODAY}").json()["edits"]) == 0
        print("✅ 撤销恢复原分类")

        # ---- 3) 删除一段 ----
        before_total = c.eval("DATA.data[CUR].total_ms")
        before_n = c.eval("TLSEGS.length")
        p = seg_point(2)
        click(p["x"], p["y"])
        c.eval("document.getElementById('epDel').click()")
        assert wait_js(f"DATA.data[CUR].total_ms < {before_total}"), "删除未生效"
        after_total = c.eval("DATA.data[CUR].total_ms")
        after_n = c.eval("TLSEGS.length")
        print(f"删除后：总时长 {before_total} → {after_total}，段数 {before_n} → {after_n}")
        assert after_n <= before_n
        print("✅ 删除该段生效")

        # ---- 4) 抽屉里的编辑记录可撤销 ----
        c.eval("(async()=>{await drill('Mozilla Firefox')})()", await_promise=True)
        time.sleep(0.9)
        n_ed = c.eval("document.querySelectorAll('#drillEdits button').length")
        print("抽屉内可撤销记录:", n_ed)
        assert n_ed >= 1
        c.shot(os.path.join(SHOT_DIR, "_e2e_drawer_edit.png"))
        c.eval("document.querySelector('#drillEdits button').click()")
        # 末段会随时间自然增长，所以用「时长回到原量级 + 段数恢复」判定
        assert wait_js(f"DATA.data[CUR].total_ms > {before_total - 60000} "
                       f"&& TLSEGS.length === {before_n}"), "抽屉撤销未恢复"
        print("✅ 抽屉撤销删除成功，时长与段数已恢复")

        print("PASS: 端到端 改分类 / 撤销 / 删除 / 抽屉撤销 全部通过")
        ws.close()
    finally:
        proc.terminate()
        time.sleep(0.6)
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/FI",
                        f"COMMANDLINE eq *{DBG}*"], capture_output=True)
        st.close()


if __name__ == "__main__":
    main()
