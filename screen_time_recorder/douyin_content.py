# -*- coding: utf-8 -*-
"""抖音内容级抓取（MVP 抖音内容级补充）。

从 probes/probe_douyin.py 提炼 UIA 遍历逻辑，针对「常驻采集」做三处关键调整：
1. **绝不抢前台**：调用方只在「抖音已是前台」时触发本函数，这里直接抓，
   不调用 SetForegroundWindow —— 否则会把用户正在使用的窗口抢走、导致闪屏/丢焦。
2. **带整体超时 + 节点上限**，防止 Chromium 无障碍树异常时卡死。
3. 只返回 content_title（视频标题+作者）字符串；任何异常/空结果都返回 None，不向外抛。

依赖 uiautomation（user site-packages，常驻进程同一解释器可 import）。
"""
import ctypes
import time

import win32gui

# uiautomation 较重，运行时惰性 import（非抖音场景不加载）
try:
    import uiautomation as uia
    uia.uiautomation.TIME_OUT = 1.5  # 单节点搜索超时，防止卡死
    _HAS_UIA = True
except Exception:
    _HAS_UIA = False


# 播放页明显的 UI 按钮/导航噪音，提取候选视频文案时排除
UI_NOISE = {
    "关注", "取消关注", "点赞", "取消点赞", "评论", "收藏", "取消收藏",
    "分享", "转发", "首页", "朋友", "我", "消息", "搜索", "直播", "推荐",
    "inbox", "私信", "+", "发布", "我的", "关注页", "发现", "同城",
    "评论区", "说点什么...", "发表评论", "展开", "收起", "更多", "停止播放",
    "暂停", "播放", "静音", "全屏", "退出全屏", "返回", "刷新", "下载",
    "＋", "作品", "喜欢", "收藏",
}

AUTHOR_HINTS = ("@", "：", ":", "抖音号", "粉丝", "获赞", "IP属地", "·")

# 窗口品牌语 / 非内容文本（开启读屏标签后根 DocumentControl 的 Name 就是品牌语）
BRAND_NOISE = {"抖音-记录美好生活", "douyin", "抖音", "抖音精选", "抖音直播"}
NOISE_CONTAINS = ("作者声明", "要获取缺失的图片说明", "打开上下文菜单", "读屏标签")


def _walk(ctrl, rows, depth, max_depth, deadline, counter):
    """递归遍历 UIA 树，收集有文本的节点。"""
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
        rows.append((depth, cty, cls[:30], name[:160]))
    try:
        children = ctrl.GetChildren()
    except Exception:
        return
    for ch in children:
        _walk(ch, rows, depth + 1, max_depth, deadline, counter)


def _clean(text):
    return " ".join((text or "").replace("\n", " ").replace("\r", " ").split())


def _is_meaningful(text):
    """是否是「视频内容类」文本（排除 UI、品牌语、纯数字、声明等）。"""
    if not text or len(text) < 6:
        return False
    if text in UI_NOISE or text in BRAND_NOISE:
        return False
    if not any('\u4e00' <= c <= '\u9fff' for c in text):
        return False
    if any(x in text for x in NOISE_CONTAINS):
        return False
    if text.replace(".", "").replace(",", "").replace(" ", "").isdigit():
        return False
    return True


def _extract_content(rows):
    """从 UIA 文本节点提取「当前视频：@作者 · 标题」。

    抖音推荐流一次会加载多条视频的作者/标题。实测树中当前视频排在前面，
    因此取「树中第一个 @作者」+「其后最近的一条标题」作为当前观看内容的近似。
    若没有作者标记，则退化为最长的一条内容文本。
    """
    cleaned = []
    for depth, cty, cls, name in rows:
        t = _clean(name)
        if t:
            cleaned.append(t)

    authors = [(i, t) for i, t in enumerate(cleaned)
               if t.startswith("@") and 2 < len(t) <= 40]
    if authors:
        ai, author = authors[0]
        for j in range(ai + 1, min(ai + 10, len(cleaned))):
            t = cleaned[j]
            if _is_meaningful(t) and len(t) >= 10:
                return f"{author} · {t}"
        return author

    titles = [t for t in cleaned if _is_meaningful(t) and len(t) >= 10]
    if titles:
        return max(titles, key=len)
    return None


# ---------- 读屏标签自愈 ----------
_last_a11y_try = 0.0


def _find_button(ctrl, name, depth=0, max_depth=6):
    """在浅层树里找指定名字的按钮（限深度，避免整树遍历）。"""
    if depth > max_depth:
        return None
    try:
        if ctrl.ControlTypeName == "ButtonControl" and name in (ctrl.Name or ""):
            return ctrl
    except Exception:
        pass
    try:
        children = ctrl.GetChildren()
    except Exception:
        return None
    for ch in children:
        r = _find_button(ch, name, depth + 1, max_depth)
        if r is not None:
            return r
    return None


def ensure_reading_tags(hwnd, min_interval=60.0):
    """抖音「读屏标签」被关闭时自动点开（内容级自愈）。

    只在内容抓取失败时调用；60s 内最多尝试一次，避免反复点击。
    返回是否实际点击。
    """
    global _last_a11y_try
    now = time.time()
    if now - _last_a11y_try < min_interval:
        return False
    _last_a11y_try = now
    if not _HAS_UIA or not hwnd or not win32gui.IsWindow(hwnd):
        return False
    try:
        ctrl = uia.ControlFromHandle(hwnd)
        btn = _find_button(ctrl, "开启读屏标签")
        if btn is None:
            return False  # 已开启（按钮叫「关闭读屏标签」）或结构变化
        try:
            ip = btn.GetInvokePattern()
            if ip:
                ip.Invoke()
            else:
                btn.Click()
        except Exception:
            btn.Click()
        return True
    except Exception:
        return False


def fetch_douyin_content(hwnd, timeout=8.0):
    """抓取抖音前台窗口的内容级标题（视频文案+作者）。返回字符串或 None。

    调用方保证 hwnd 是抖音前台窗口；本函数不抢前台、不改动窗口状态。
    """
    if not _HAS_UIA:
        return None
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    # 子线程调用 COM/UIA 需要本线程初始化公寓（STA 推荐）
    try:
        ctypes.windll.ole32.CoInitializeEx(0, 2)
    except Exception:
        pass
    try:
        ctrl = uia.ControlFromHandle(hwnd)
        if ctrl is None:
            return None
        # 抖音有两个 Chrome_RenderWidgetHostHWND：第一个承载 web 内容，第二个为空。
        # 必须全部遍历并合并（只取最后一个会得到空树）。
        render_ctrls = []
        try:
            for ch in ctrl.GetChildren():
                if "RenderWidget" in str(ch.ClassName):
                    render_ctrls.append(ch)
        except Exception:
            render_ctrls = []
        rows = []
        counter = {"n": 0, "max": 2500}
        t0 = time.time()
        if render_ctrls:
            for rc in render_ctrls:
                _walk(rc, rows, 0, 18, t0 + timeout, counter)
        # 渲染窗口无内容则回退从根遍历
        if not rows:
            counter["n"] = 0
            _walk(ctrl, rows, 0, 18, time.time() + timeout, counter)
        return _extract_content(rows)
    except Exception:
        return None
