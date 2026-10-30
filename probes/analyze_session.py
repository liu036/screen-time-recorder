# -*- coding: utf-8 -*-
"""
采集会话分析器 —— 从 session.jsonl 提取 T3/T6/T8 的结论
用法: python analyze_session.py [session_dir]
"""
import glob
import io
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def hr(t):
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


def load(d):
    p = os.path.join(d, "session.jsonl")
    return [json.loads(line) for line in open(p, encoding="utf-8")]


def main():
    if len(sys.argv) > 1:
        d = sys.argv[1]
    else:
        ds = [x for x in glob.glob(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "sessions", "2*")) if os.path.isdir(x)]
        d = max(ds, key=os.path.getmtime)

    rs = load(d)
    t0, t1 = rs[0]["t"], rs[-1]["t"]
    dur_min = (t1 - t0) / 60000
    by = defaultdict(list)
    for r in rs:
        by[r["type"]].append(r)

    print(f"会话目录: {os.path.basename(d)}")
    print(f"时长: {dur_min:.1f} 分钟   记录总数: {len(rs)}")

    # ---------------------------------------------------------- 1. 对拍
    hr("1. 事件驱动 (SetWinEventHook) vs 250ms 轮询 —— 对拍")

    ev = [(r["t"], r.get("hwnd"), r.get("proc"), r.get("title", "")) for r in by["fg_event"]]
    pl = [(r["t"], r.get("hwnd"), r.get("proc"), r.get("title", "")) for r in by["fg_poll"]]
    print(f"事件驱动捕获: {len(ev)} 条    轮询捕获: {len(pl)} 条")

    # 去重：连续相同 hwnd 只算一次（事件可能对同一窗口重复触发）
    def collapse(seq):
        out = []
        for t, h, p, ti in seq:
            if not out or out[-1][1] != h:
                out.append((t, h, p, ti))
        return out

    ev_c, pl_c = collapse(ev), collapse(pl)
    print(f"折叠连续重复后:  事件 {len(ev_c)}    轮询 {len(pl_c)}")

    # 匹配：事件驱动每条，找 ±600ms 内同 hwnd 的轮询记录
    matched = 0
    only_ev = []
    for t, h, p, ti in ev_c:
        if any(abs(tp - t) <= 600 and hp == h for tp, hp, _, _ in pl_c):
            matched += 1
        else:
            only_ev.append((t, h, p, ti))
    only_pl = []
    for t, h, p, ti in pl_c:
        if not any(abs(te - t) <= 600 and he == h for te, he, _, _ in ev_c):
            only_pl.append((t, h, p, ti))

    if ev_c:
        print(f"\n事件驱动命中轮询: {matched}/{len(ev_c)} ({matched/len(ev_c)*100:.1f}%)")
    print(f"仅事件驱动捕获（轮询漏掉）: {len(only_ev)} 条  → 这些是<250ms 的快速切换")
    for t, h, p, ti in only_ev[:12]:
        print(f"    +{(t-t0)/1000:7.1f}s  {str(p)[:20]:<21} {ti[:36]!r}")
    print(f"仅轮询捕获（事件漏掉）: {len(only_pl)} 条")
    for t, h, p, ti in only_pl[:12]:
        print(f"    +{(t-t0)/1000:7.1f}s  {str(p)[:20]:<21} {ti[:36]!r}")

    # ---------------------------------------------------------- 2. 停留时长分布
    hr("2. 前台停留时长分布 —— 决定 <2s 丢弃阈值 (F10)")
    seq = ev if len(ev) >= len(pl) else pl
    seq = collapse(seq)
    durs = []
    for i in range(len(seq) - 1):
        durs.append((seq[i + 1][0] - seq[i][0]) / 1000.0)
    if durs:
        durs_s = sorted(durs)
        n = len(durs_s)

        def pct(x):
            return durs_s[min(n - 1, int(n * x))]

        print(f"切换段数: {n}")
        print(f"  最短 {durs_s[0]:.2f}s   中位 {pct(0.5):.2f}s   "
              f"P25 {pct(0.25):.2f}s   P90 {pct(0.9):.2f}s   最长 {durs_s[-1]:.1f}s")
        total = sum(durs_s)
        print()
        print("  阈值      丢弃段数   占比     丢弃的总时长     占总时长")
        for th in (0.5, 1.0, 2.0, 3.0, 5.0):
            drop = [x for x in durs_s if x < th]
            print(f"  < {th:<5.1f}s  {len(drop):>4}      {len(drop)/n*100:5.1f}%   "
                  f"{sum(drop):>8.1f}s        {sum(drop)/total*100:5.2f}%")
        print()
        print(f"  总记录时长（前台切换覆盖）: {total:.0f}s = {total/60:.1f} 分钟")

    # ---------------------------------------------------------- 3. 锁屏
    hr("3. 锁屏 / 无前台窗口事件 (F3)")
    if by["lock"]:
        for r in by["lock"]:
            print(f"  +{(r['t']-t0)/1000:7.1f}s  locked={r['locked']}")
        print(f"  → 共 {len(by['lock'])} 次状态变化，锁屏检测可用")
    else:
        print("  本次会话未发生锁屏（未触发测试动作）")

    # ---------------------------------------------------------- 4. 可见性快照
    hr("4. 窗口布局快照 —— 分屏 / 噪音窗口 (Q5, Q10)")
    snaps = by["snap"]
    print(f"快照数: {len(snaps)}   平均间隔: {dur_min*60/max(1,len(snaps)):.1f}s")
    vis_counts = [s["visible_count"] for s in snaps]
    if vis_counts:
        print(f"可见窗口数: 最少 {min(vis_counts)}  中位 "
              f"{statistics.median(vis_counts):.0f}  最多 {max(vis_counts)}")

    # 噪音窗口统计
    proc_hit = Counter()
    for s in snaps:
        for w in s["windows"]:
            if w["visible"] and not w["iconic"] and not w["cloaked"]:
                proc_hit[w["proc"]] += 1
    print("\n窗口出现频次（所有快照，已排除最小化/cloaked）：")
    for p, c in proc_hit.most_common(15):
        print(f"    {p[:32]:<33} {c:>4} 次")

    # 分屏检测：同一快照中 cover>=0.25 的窗口 >=2
    split = []
    for s in snaps:
        ws = [w for w in s["windows"]
              if w["visible"] and not w["iconic"] and not w["cloaked"] and w.get("cover", 0) >= 0.25]
        if len(ws) >= 2:
            split.append((s, ws))
    print(f"\n疑似分屏/多窗口同屏的快照: {len(split)} 条")
    for s, ws in split[:3]:
        print(f"  +{(s['t']-t0)/1000:7.1f}s  同屏 {len(ws)} 个窗口:")
        for w in ws:
            print(f"      {w['proc'][:20]:<21} cover={w['cover']:<6} rect={w['rect']} "
                  f"{w['title'][:30]!r}")
    if not split:
        print("  未检测到分屏（可能未执行二分屏动作）")

    # 最小化窗口污染
    iconic = [s["iconic_count"] for s in snaps]
    print(f"\n最小化窗口数（若不过滤会误判为可见）: 平均 "
          f"{statistics.mean(iconic):.1f}  最多 {max(iconic) if iconic else 0}")

    # ---------------------------------------------------------- 5. 资源
    hr("5. 资源占用 (Q6)")
    cpus = [r["cpu"] for r in by["res"] if r.get("cpu") is not None]
    rss = [r["rss_mb"] for r in by["res"] if r.get("rss_mb")]
    if cpus:
        print(f"采样 {len(cpus)} 次")
        print(f"  CPU%   平均 {statistics.mean(cpus):.2f}   中位 "
              f"{statistics.median(cpus):.2f}   最大 {max(cpus):.2f}")
        print(f"  内存MB 平均 {statistics.mean(rss):.1f}   中位 "
              f"{statistics.median(rss):.1f}   最大 {max(rss):.1f}")
        print(f"  线程数 {by['res'][-1].get('threads')}")

    # ---------------------------------------------------------- 6. 容量推算
    hr("6. 数据量推算（基于本次实测）")
    size = os.path.getsize(os.path.join(d, "session.jsonl"))
    per_day = size / dur_min * 60 * 16 / 1048576  # 按每天 16 小时使用
    print(f"本次 {size/1048576:.2f} MB / {dur_min:.1f} 分钟")
    print(f"按每天 16 小时使用推算: {per_day:.2f} MB/天 → {per_day*365:.0f} MB/年")
    n_per_day = len(rs) / dur_min * 60 * 16
    print(f"记录条数: {n_per_day:.0f} 条/天 → {n_per_day*365/10000:.1f} 万条/年")
    print(f"  (快照全量存储，含每个窗口完整信息；实际产品可只存变化量)")


if __name__ == "__main__":
    main()
