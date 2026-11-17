# -*- coding: utf-8 -*-
"""截图验证仪表盘时间线上的「专注高亮」。

用 CDP 驱动无头 Chrome 打开本地仪表盘，确认 DATA 里当天有 focus 时段，
并且 SVG 里画出了金色底纹，最后截整页图。

运行：python probes/ui_focus_overlay.py
"""
import base64
import json
import os
import subprocess
import sys
import time

import requests
import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "http://127.0.0.1:8765/"
PORT = 9334
SHOT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(SHOT_DIR, "_cdp_profile_focus")


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.i = 0

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method,
                                 "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def eval(self, expr, await_promise=False):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("text"))
        return r.get("result", {}).get("value")

    def shot(self, path, full=True, clip=None):
        params = {"format": "png", "captureBeyondViewport": full}
        if clip:
            params["clip"] = clip
        r = self.send("Page.captureScreenshot", **params)
        open(path, "wb").write(base64.b64decode(r["data"]))


def main():
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu",
         "--force-prefers-reduced-motion", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions",
         "--remote-debugging-port=%d" % PORT, "--remote-allow-origins=*",
         "--user-data-dir=%s" % PROFILE, "--window-size=1280,980",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(60):
            try:
                for t in requests.get(
                        "http://127.0.0.1:%d/json" % PORT, timeout=2).json():
                    if t.get("type") == "page":
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        assert ws_url, "Chrome 调试端口未就绪"

        page = CDP(websocket.create_connection(ws_url, timeout=15))
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Page.navigate", url=URL)

        focus_n = -1
        for _ in range(60):
            time.sleep(0.25)
            try:
                day = page.eval("CUR")
                focus_n = page.eval(
                    "((DATA.data[CUR]||{}).focus||[]).length")
                ready = page.eval(
                    "document.querySelectorAll('#timeline rect').length")
            except Exception:
                continue
            if ready and focus_n >= 0:
                break
        print("当天:", day, "focus 段:", focus_n,
              "timeline rects:", page.eval(
                  "document.querySelectorAll('#timeline rect').length"))
        assert focus_n > 0, "今天没有专注会话，无法验证高亮（先跑一轮番茄钟）"

        # 专注轨道的 rect 数量应与 focus 段一致（.fband 只给轨道用）
        gold = page.eval(
            "document.querySelectorAll('#timeline rect.fband').length")
        print("专注轨道 rect:", gold)
        assert gold == focus_n, (gold, focus_n)
        stroke_n = page.eval(
            "Array.from(document.querySelectorAll('#timeline rect'))"
            ".filter(r=>r.getAttribute('stroke')==='#D4A017').length")
        print("金色描边 rect:", stroke_n)
        assert stroke_n == focus_n, (stroke_n, focus_n)

        legend = page.eval("document.getElementById('sTl').textContent")
        print("图例:", legend[:120])
        assert "金色轨" in legend

        page.shot(os.path.join(SHOT_DIR, "_focus_overlay.png"))
        clip = page.eval(
            "(()=>{const r=document.getElementById('timelineCard')"
            ".getBoundingClientRect();"
            "return {x:r.x,y:r.y,width:r.width,height:r.height}})()")
        clip["scale"] = 2
        page.shot(os.path.join(SHOT_DIR, "_focus_timeline.png"), clip=clip)
        print("截图: probes/_focus_overlay.png / _focus_timeline.png")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
