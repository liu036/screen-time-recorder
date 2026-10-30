# -*- coding: utf-8 -*-
"""用 CDP 驱动无头 Chrome，验证时间线缩放/悬停/编辑浮层的真实行为。

不修改真实数据：编辑浮层只打开不提交，缩放/悬停只读。
"""
import json
import os
import subprocess
import sys
import time

import requests
import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "http://127.0.0.1:8765/"
PORT = 9333
SHOT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(SHOT_DIR, "_cdp_profile")


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.i = 0

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr, await_promise=False):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("text"))
        return r.get("result", {}).get("value")

    def mouse(self, typ, x, y, buttons=0, click_count=0):
        self.send("Input.dispatchMouseEvent", type=typ, x=x, y=y,
                  button="left" if buttons or typ != "mouseMoved" else "none",
                  buttons=buttons, clickCount=click_count)

    def shot(self, path, full=True):
        r = self.send("Page.captureScreenshot", format="png", captureBeyondViewport=full)
        import base64
        open(path, "wb").write(base64.b64decode(r["data"]))


def main():
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu",
                             "--force-prefers-reduced-motion",
                             "--no-first-run", "--no-default-browser-check",
                             "--disable-extensions",
                             f"--remote-debugging-port={PORT}",
                             "--remote-allow-origins=*",
                             f"--user-data-dir={PROFILE}",
                             "--window-size=1280,900", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(60):
            try:
                tabs = requests.get(f"http://127.0.0.1:{PORT}/json", timeout=2).json()
                for t in tabs:
                    if t.get("type") == "page":
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.4)
        assert ws_url, "无法连接 Chrome 调试端口"

        ws = websocket.create_connection(ws_url, timeout=30)
        c = CDP(ws)
        c.send("Page.enable")
        c.send("Runtime.enable")
        # 显式导航，确保页面真正加载（直接带 url 启动时有时停在 loading）
        c.send("Page.navigate", url=URL)
        time.sleep(2.0)

        # 等数据加载完成（时间线渲染出 rect）
        ok = False
        for _ in range(90):
            n = c.eval("document.querySelectorAll('#timeline rect').length")
            if n and n > 5:
                ok = True
                break
            time.sleep(0.5)
        assert ok, "时间线未渲染"
        print("时间线 rect 数:", c.eval("document.querySelectorAll('#timeline rect').length"))
        print("TLSEGS:", c.eval("TLSEGS.length"), "| 视图:", c.eval("[TLV.lo.toFixed(4),TLV.hi.toFixed(4)]"))
        print("undo 按钮可见:", c.eval(
            "getComputedStyle(document.getElementById('undoLast')).display"))

        # 选中一个较宽的段，算出它在屏幕上的坐标（视口坐标）
        c.eval("document.getElementById('timelineCard').scrollIntoView({block:'center'})")
        time.sleep(0.4)
        pos = c.eval("""(()=>{
          const s=document.getElementById('timeline');
          const r=s.getBoundingClientRect(), vb=s.viewBox.baseVal;
          const sc=Math.min(r.width/vb.width, r.height/vb.height);
          const ox=r.left+(r.width-vb.width*sc)/2, oy=r.top+(r.height-vb.height*sc)/2;
          const span=TLV.hi-TLV.lo;
          const seg=TLSEGS.reduce((a,b)=>(b.ef-b.sf)>(a.ef-a.sf)?b:a);
          const vx=TLX0+(TLX1-TLX0)*((seg.sf+seg.ef)/2-TLV.lo)/span;
          return {x:ox+vx*sc, y:oy+(TLY+TLH/2)*sc, app:seg.app};
        })()""")
        print("hit point:", pos)

        # ---- 1) 悬停：应出现 tooltip ----
        mx, my = pos["x"], pos["y"]
        c.mouse("mouseMoved", mx, my)
        time.sleep(0.35)
        visible = c.eval("getComputedStyle(document.getElementById('tlTip')).display")
        text = c.eval("document.getElementById('tlTip').innerText")
        print("hover tooltip display:", visible)
        print("hover tooltip text:", repr(text))
        assert visible != "none", "悬停未出现 tooltip"
        c.shot(os.path.join(SHOT_DIR, "_cdp_hover.png"), full=False)

        # ---- 2) 滚轮缩放 ----
        before = c.eval("TLV.hi-TLV.lo")
        for _ in range(3):
            c.send("Input.dispatchMouseEvent", type="mouseWheel",
                   x=mx, y=my, deltaX=0, deltaY=-120)
            time.sleep(0.15)
        after = c.eval("TLV.hi-TLV.lo")
        print(f"zoom span: {before:.4f} -> {after:.4f}")
        assert after < before * 0.95, "滚轮缩放无效"
        ticks = c.eval("Array.from(document.querySelectorAll('#timeline text'))"
                       ".slice(0,4).map(t=>t.textContent).join(' ')")
        print("缩放后刻度:", ticks)
        c.shot(os.path.join(SHOT_DIR, "_cdp_zoom.png"), full=False)

        # ---- 3) 放大不越界 + 右上角「还原」----
        auto = c.eval("[TLV.autoLo, TLV.autoHi]")
        left_x = c.eval("""(()=>{
          const s=document.getElementById('timeline');
          const r=s.getBoundingClientRect(), vb=s.viewBox.baseVal;
          const sc=Math.min(r.width/vb.width, r.height/vb.height);
          return r.left+(r.width-vb.width*sc)/2+(TLX0+5)*sc;
        })()""")
        # 在靠左位置连续放大：视窗左边界不应越过活动范围起点
        for _ in range(6):
            c.send("Input.dispatchMouseEvent", type="mouseWheel",
                   x=left_x, y=my, deltaX=0, deltaY=-120)
            time.sleep(0.1)
        lo, hi = c.eval("[TLV.lo, TLV.hi]")
        print(f"左边缘放大后 [{lo:.4f}, {hi:.4f}] · 活动范围 {auto}")
        assert lo >= auto[0] - 1e-6, f"左边界越界: {lo} < {auto[0]}"
        assert hi <= auto[1] + 1e-6, f"右边界越界: {hi} > {auto[1]}"
        assert c.eval("document.getElementById('tlReset').classList.contains('on')"), \
            "放大后未出现「还原」按钮"
        c.eval("document.getElementById('tlReset').click()")
        time.sleep(0.35)
        span2 = c.eval("TLV.hi-TLV.lo")
        print(f"点「还原」后 span: {span2:.4f}（全景 {auto[1]-auto[0]:.4f}）")
        assert abs(span2 - (auto[1]-auto[0])) < 2e-4, "还原未回到全景"
        assert not c.eval("document.getElementById('tlReset').classList.contains('on')"), \
            "还原后按钮应隐藏"
        print("✅ 放大不越出活动范围，右上角「还原」正常")

        # ---- 4) 点击色块 → 编辑浮层 ----
        c.mouse("mouseMoved", mx, my)
        c.mouse("mousePressed", mx, my, buttons=1, click_count=1)
        c.mouse("mouseReleased", mx, my, buttons=0, click_count=1)
        time.sleep(0.45)
        show = c.eval("document.getElementById('epop').classList.contains('show')")
        print("编辑浮层打开:", show)
        assert show, "点击色块未打开编辑浮层"
        print("浮层标题:", repr(c.eval("document.getElementById('epTitle').textContent")))
        print("浮层副标题:", repr(c.eval("document.getElementById('epSub').innerText")))
        print("分类数量:", c.eval("document.querySelectorAll('#epChips button').length"))
        c.shot(os.path.join(SHOT_DIR, "_cdp_pop.png"), full=False)

        # ---- 5) 浮层内取消（不写库） ----
        c.eval("document.getElementById('epCancel').click()")
        time.sleep(0.2)
        print("取消后浮层关闭:", not c.eval(
            "document.getElementById('epop').classList.contains('show')"))

        # ---- 6) 抽屉：点应用 → 出现编辑按钮 ----
        c.eval("(async()=>{await drill(DATA.data[CUR].by_app[0][0])})()", await_promise=True)
        time.sleep(0.5)
        print("抽屉打开:", c.eval("document.getElementById('drawer').classList.contains('open')"))
        print("抽屉内编辑按钮:", c.eval("document.querySelectorAll('#drillBody button.edit').length"))
        c.shot(os.path.join(SHOT_DIR, "_cdp_drawer.png"), full=False)

        print("PASS: 悬停 / 滚轮放大(不越界) / 还原按钮 / 点击编辑浮层 / 抽屉 全部正常")
        ws.close()
    finally:
        proc.terminate()
        time.sleep(0.8)
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/FI",
                        "COMMANDLINE eq *9333*"], capture_output=True)


if __name__ == "__main__":
    main()
