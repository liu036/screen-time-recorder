# -*- coding: utf-8 -*-
"""
T9 · 抖音 UIA 探测
验证能否通过 UIA（无障碍树）取到抖音客户端的内容级信息。

用法:
  python probe_douyin.py --list          只列出窗口，不动窗口
  python probe_douyin.py --probe         恢复窗口 + UIA 遍历 + 恢复原状
"""
import argparse
import ctypes
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import win32con
import win32gui
import win32process
import psutil
import uiautomation as uia

uia.uiautomation.TIME_OUT = 1.5  # 单节点搜索超时，防止卡死


def find_douyin_windows():
    procs = {p.info["pid"] for p in psutil.process_iter(["pid", "name"])
             if p.info["name"] and "douyin" in p.info["name"].lower()}
    rows = []

    def cb(h, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if pid not in procs or not win32gui.IsWindowVisible(h):
                return True
            t = win32gui.GetWindowText(h)
            c = win32gui.GetClassName(h)
            if not t and not c:
                return True
            l, tp, r, b = win32gui.GetWindowRect(h)
            rows.append({"hwnd": h, "pid": pid, "title": t, "cls": c,
                         "iconic": bool(win32gui.IsIconic(h)),
                         "rect": (l, tp, r - l, b - tp)})
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return rows


def walk(ctrl, rows, depth, max_depth, deadline, counter):
    """递归遍历 UIA 树，收集有文本的节点"""
    if depth > max_depth or time.time() > deadline or counter["n"] > counter["max"]:
        return
    try:
        name = (ctrl.Name or "").strip()
        cty = ctrl.ControlTypeName
        cls = ctrl.ClassName or ""
    except Exception:
        return
    counter["n"] += 1
    if name and counter["n"] <= counter["max"]:
        rows.append((depth, cty, cls[:30], name[:120]))
    try:
        children = ctrl.GetChildren()
    except Exception:
        return
    for ch in children:
        walk(ch, rows, depth + 1, max_depth, deadline, counter)


# 播放页明显的 UI 按钮/导航噪音，提取候选视频文案时排除
UI_NOISE = {
    "关注", "取消关注", "点赞", "取消点赞", "评论", "收藏", "取消收藏",
    "分享", "转发", "首页", "朋友", "我", "消息", "搜索", "直播", "推荐",
    " inbox", "私信", "+", "发布", "我的", "关注页", "发现", "同城",
    "评论区", "说点什么...", "发表评论", "展开", "收起", "更多", "停止播放",
    "暂停", "播放", "静音", "全屏", "退出全屏", "返回", "刷新", "下载",
}

AUTHOR_HINTS = ("@", "：", ":", "抖音号", "粉丝", "获赞", "IP属地")


def extract_video_candidates(rows):
    """从遍历结果里挑出最可能代表「当前播放视频」的文本。

    规则：
    1. 长度 >= 12 且含中文
    2. 不是 UI 按钮噪音
    3. 优先：来自 DocumentControl（当前视频区）/ 含作者或话题标记
    """
    cands = []
    for depth, cty, cls, name in rows:
        if len(name) < 12:
            continue
        if not any('\u4e00' <= c <= '\u9fff' for c in name):
            continue
        if name in UI_NOISE or name.strip() in UI_NOISE:
            continue
        # 排除纯数字/时间/点赞数
        if name.replace(".", "").replace(",", "").replace(" ", "").isdigit():
            continue
        score = 0
        if cty == "DocumentControl":
            score += 3
        if any(h in name for h in AUTHOR_HINTS):
            score += 2
        if "#" in name:
            score += 1
        cands.append((score, depth, cty, name))
    # 去重 + 按分数降序
    seen = set()
    uniq = []
    for s, d, c, n in sorted(cands, key=lambda x: -x[0]):
        if n in seen:
            continue
        seen.add(n)
        uniq.append((s, d, c, n))
    return uniq[:15]


def probe(hwnd, was_iconic):
    # 1. 恢复窗口
    if was_iconic:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(3)
    # 抖一下前台（Chromium 需要 focus 才渲染 accessibility 树）
    for attempt in range(3):
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(1)
        fg = win32gui.GetForegroundWindow()
        print(f"  前台尝试 {attempt+1}: GetForegroundWindow={fg} "
              f"{'✅' if fg == hwnd else '❌'}")
        if fg == hwnd:
            break
    time.sleep(2)

    ctrl = uia.ControlFromHandle(hwnd)
    try:
        print(f"根控件: Name={ctrl.Name!r}  Type={ctrl.ControlTypeName}  "
              f"Class={ctrl.ClassName!r}  Framework={ctrl.FrameworkId!r}")
    except Exception as e:
        print(f"根控件读取失败: {e}")
        return [], was_iconic

    # 检查第一层子结构（找渲染窗口）
    print("\n第一层子控件:")
    render_ctrl = None
    try:
        for ch in ctrl.GetChildren():
            print(f"    Type={ch.ControlTypeName:<16} Class={str(ch.ClassName)[:34]:<35} "
                  f"Name={str(ch.Name)[:40]!r}")
            if "RenderWidget" in str(ch.ClassName):
                render_ctrl = ch
    except Exception as e:
        print(f"    枚举失败: {e}")

    rows = []
    counter = {"n": 0, "max": 3000}
    t_start = time.time()

    # 从渲染窗口进入（Chromium 的 web 内容在这里）
    if render_ctrl is not None:
        print("\n>>> 从 Chrome_RenderWidgetHostHWND 进入 web 内容树")
        walk(render_ctrl, rows, 0, max_depth=16, deadline=t_start + 45, counter=counter)

    # 渲染窗口没内容的话，从根再来一遍
    if not rows:
        print(">>> 渲染窗口无内容，回退从根遍历")
        counter["n"] = 0
        walk(ctrl, rows, 0, max_depth=16, deadline=time.time() + 45, counter=counter)

    print(f"遍历完成: 访问节点 {counter['n']}，有文本节点 {len(rows)}，"
          f"耗时 {time.time()-t_start:.1f}s")
    return rows, was_iconic


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    wins = find_douyin_windows()
    print(f"抖音可见窗口: {len(wins)}")
    for w in wins:
        print(f"  hwnd={w['hwnd']:<9} 最小化={w['iconic']} cls={w['cls'][:26]:<27} "
              f"rect={w['rect']} 标题={w['title'][:36]!r}")

    if args.list or not wins:
        if not wins:
            print("\n⚠️ 没找到抖音可见窗口。请确认：抖音已打开、未最小化、"
                  "且当前在播放某个视频。")
        return

    target = wins[0]
    print(f"\n探测目标: hwnd={target['hwnd']}  当前标题={target['title']!r}")

    rows, was_iconic = probe(target["hwnd"], target["iconic"])

    # 打印有文本的节点
    print("\n=== UIA 树中的文本节点（按深度）===")
    for depth, cty, cls, name in rows[:120]:
        print(f"  {'  '*depth}{cty:<14} {name!r}")

    # 候选视频文案（播放页重点）
    cands = extract_video_candidates(rows)
    print("\n=== 🎬 候选「当前播放视频」信息（按可信度排序）===")
    if not cands:
        print("  ⚠️ 未提取到候选视频文案 —— 可能当前不在视频播放页，"
              "或内容未渲染")
    for score, depth, cty, name in cands:
        print(f"  [score={score}] {cty:<14} {name!r}")

    if args.out and rows:
        with open(args.out, "w", encoding="utf-8") as f:
            for depth, cty, cls, name in rows:
                f.write(f"{depth}\t{cty}\t{cls}\t{name}\n")
        print(f"\n完整结果已存: {args.out}")

    # 恢复原状
    if was_iconic:
        win32gui.ShowWindow(target["hwnd"], win32con.SW_MINIMIZE)
        print("(窗口已恢复最小化)")


if __name__ == "__main__":
    main()
