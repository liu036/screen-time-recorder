# -*- coding: utf-8 -*-
"""补充分析：噪音窗口成分 / 真实停留分布 / 分屏质量 / 锁屏信号"""
import glob
import io
import json
import os
import sys
from collections import Counter, defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

d = max([x for x in glob.glob(r"D:\笔记\屏幕时间记录器\probes\sessions\2*")
         if os.path.isdir(x)], key=os.path.getmtime)
rs = [json.loads(l) for l in open(os.path.join(d, "session.jsonl"), encoding="utf-8")]
t0 = rs[0]["t"]
by = defaultdict(list)
for r in rs:
    by[r["type"]].append(r)

hr = lambda t: print("\n" + "=" * 66 + "\n" + t + "\n" + "=" * 66)

hr("A. explorer.exe 空标题事件来源（20 次，需判定是否为噪音）")
n = 0
for r in by["fg_event"]:
    if r.get("proc") == "explorer.exe" and not (r.get("title") or ""):
        n += 1
        if n <= 10:
            print(f"  +{(r['t']-t0)/1000:7.2f}s hwnd={r.get('hwnd'):<9} "
                  f"cls={str(r.get('cls'))[:24]:<25} rect={r.get('rect')}")
print(f"  共 {n} 条")

hr("B. 真实应用切换的停留时长分布（剔除系统窗口后）")
NOISE_CLS = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
             "Windows.UI.Core.CoreWindow", "XamlWindow"}
NOISE_TITLE = {"任务切换", "UnlockingWindow", "Program Manager", "贴靠助手",
               "Windows 输入体验", "搜索", "开始", "任务视图"}
NOISE_PROC = {"TextInputHost.exe", "LockApp.exe", "NVIDIA Overlay.exe",
              "SearchHost.exe", "ShellExperienceHost.exe", "StartMenuExperienceHost.exe"}

seq = []
for r in by["fg_event"]:
    p, t = r.get("proc"), (r.get("title") or "")
    if p in NOISE_PROC:
        continue
    if t in NOISE_TITLE:
        continue
    if p == "explorer.exe" and not t:
        continue
    if r.get("cls") in NOISE_CLS:
        continue
    if not seq or seq[-1][1] != r.get("hwnd"):
        seq.append((r["t"], r.get("hwnd"), p, t))

print(f"真实切换段数: {len(seq)}   （原始 fg_event {len(by['fg_event'])} 条）")
print(f"被判定为系统噪音: {len(by['fg_event'])-len(seq)} 条 "
      f"({(len(by['fg_event'])-len(seq))/max(1,len(by['fg_event']))*100:.0f}%)")
durs = [(seq[i + 1][0] - seq[i][0]) / 1000.0 for i in range(len(seq) - 1)]
if durs:
    s = sorted(durs)
    n = len(s)
    print(f"\n  停留时长: 最短 {s[0]:.2f}s  P25 {s[n//4]:.2f}s  中位 {s[n//2]:.2f}s  "
          f"P75 {s[n*3//4]:.2f}s  P90 {s[int(n*0.9)]:.2f}s  最长 {s[-1]:.1f}s")
    tot = sum(s)
    print("\n  阈值      丢弃段数   占比    损失时长    占总时长    剩余段数")
    for th in (0.5, 1.0, 2.0, 3.0, 5.0):
        drop = [x for x in s if x < th]
        print(f"  < {th:<5.1f}s  {len(drop):>5}    {len(drop)/n*100:5.1f}%  "
              f"{sum(drop):>7.1f}s    {sum(drop)/tot*100:5.2f}%     {n-len(drop)}")
    print(f"\n  时间线明细（前 15 段）:")
    for i in range(min(15, len(seq) - 1)):
        print(f"    +{(seq[i][0]-t0)/1000:7.1f}s  {durs[i]:>6.1f}s  "
              f"{str(seq[i][2])[:18]:<19} {seq[i][3][:34]!r}")

hr("C. 分屏时段的完整窗口清单（看噪音污染程度）")
target = None
for r in by["snap"]:
    ws = r["windows"]
    has_left = any(w["rect"][0] == 0 and w["rect"][2] == 1280 for w in ws)
    has_right = any(w["rect"][0] == 1280 and w["rect"][2] == 1280 for w in ws)
    if has_left and has_right:
        target = r
        break
if target:
    print(f"快照 +{(target['t']-t0)/1000:.1f}s  可见 {target['visible_count']} / "
          f"总计 {target['total_count']}  最小化 {target['iconic_count']}")
    for w in sorted(target["windows"], key=lambda x: -x.get("cover", 0)):
        flags = []
        if w["iconic"]:
            flags.append("最小化")
        if w["cloaked"]:
            flags.append("cloaked")
        if not w["visible"]:
            flags.append("不可见")
        print(f"  {str(w['proc'])[:20]:<21} cls={str(w['cls'])[:18]:<19} "
              f"cover={w.get('cover',0):<5} rect={str(w['rect']):<26} "
              f"{'/'.join(flags):<10} {w['title'][:26]!r}")
else:
    print("未找到左右分屏快照")

hr("D. 锁屏相关信号")
for r in by["fg_event"]:
    if "LockApp" in str(r.get("proc")) or "Unlocking" in str(r.get("title")):
        print(f"  +{(r['t']-t0)/1000:7.1f}s  {r.get('proc'):<16} cls={str(r.get('cls'))[:20]:<21} "
              f"{r.get('title','')!r}")
print("  --- lock 事件(轮询检测) ---")
for r in by["lock"]:
    print(f"  +{(r['t']-t0)/1000:7.1f}s  locked={r['locked']}")
print("  --- 锁屏前后的 idle 变化 ---")
for r in by["fg_beat"]:
    print(f"  +{(r['t']-t0)/1000:7.1f}s  hwnd={r.get('hwnd')}  idle={r.get('idle_ms')}ms")

hr("E. 窗口身份稳定性（Q11：hwnd 是否会复用/漂移）")
hwnd_proc = defaultdict(set)
hwnd_title = defaultdict(set)
for r in by["fg_event"]:
    if r.get("hwnd"):
        hwnd_proc[r["hwnd"]].add(r.get("proc"))
        hwnd_title[r["hwnd"]].add((r.get("title") or "")[:30])
drift = {h: p for h, p in hwnd_proc.items() if len(p) > 1}
print(f"出现过的 hwnd 数: {len(hwnd_proc)}")
print(f"同一 hwnd 对应多个进程的（pid 复用）: {len(drift)}")
for h, p in list(drift.items())[:5]:
    print(f"    hwnd={h}: {p}")
multi = {h: t for h, t in hwnd_title.items() if len(t) > 1}
print(f"\n同一 hwnd 出现多个标题的（内容变化）: {len(multi)}")
for h, t in list(multi.items())[:8]:
    print(f"    hwnd={h}:")
    for x in list(t)[:4]:
        print(f"        {x!r}")
