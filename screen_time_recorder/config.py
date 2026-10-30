# -*- coding: utf-8 -*-
"""MVP 配置 —— 所有参数均来自《3-技术调研报告》实测结论，无臆测。

关键来源：
- 噪音窗口：T5 实测（前台事件流 67% 是系统窗口，需进程名+类名双重黑名单）
- <2s 阈值：T3 实测（先过滤噪音再套阈值，否则中位停留被低估到 0.12s）
- 可见性三重判据：T6 实测（cover 指标失效，最小化窗口 IsWindowVisible 仍 True）
- AFK/锁屏：T3 实测（LockApp.exe + hwnd==0，需 debounce）
- 容量优化：T3 实测（2392MB/年 → 抽屉式快照 + apps 去重）
"""
import os

# ---------- 路径 ----------
DATA_DIR = os.path.join(os.path.expanduser("~"), ".screen_time_recorder")
DB_PATH = os.path.join(DATA_DIR, "events.db")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------- 采集节奏 ----------
HOOK_POLL_MS = 250          # 轮询兜底间隔（事件驱动为主，轮询补漏）
IDLE_POLL_MS = 1000         # AFK 空闲检测间隔
SNAPSHOT_INTERVAL_MS = 1000  # 可见性快照：1s 检测布局，变化才写
HEARTBEAT_MS = 60000        # 保活心跳（事件驱动 + 60s 心跳，非 1s 采样）

# ---------- 产品决策参数 ----------
MIN_ACTIVITY_DURATION_MS = 60000  # 非内容活动段低于 1 分钟不记录（应用级碎片）
MIN_CONTENT_DISPLAY_MS = 60000    # 内容明细低于 1 分钟不展示（时长仍计入应用统计）
SWITCH_MIN_DURATION_MS = 2000     # 历史阈值（<2s 合并），当前不再使用
AFK_THRESHOLD_MS = 90000          # 默认 90s 无输入判定空闲（F2）

# ---------- 噪音过滤：进程名黑名单 ----------
# 这些进程下的窗口一律不算"应用"（T5 实测：输入体验/Overlay/IME 宿主等）
NOISE_PROC = {
    "NVIDIA Overlay.exe",
    "TextInputHost.exe",     # Windows 输入体验（全屏 2560x1440 假可见）
    "ShellExperienceHost.exe",
    "SearchUI.exe",
    "SearchApp.exe",
    "SearchHost.exe",              # Win11 搜索宿主
    "StartMenuExperienceHost.exe", # Win11 开始菜单
    "dllhost.exe",
    "RuntimeBroker.exe",
    "sihost.exe",
}

# ---------- 噪音过滤：进程名关键字（版本变化也能匹配）----------
# 常驻桌面工具 / 桌宠：用户明确要求不算作屏幕使用时间
NOISE_PROC_KEYWORDS = ("dsh-pet", "pixpin", "pythonw", "windowsterminal")

# ---------- 噪音过滤：窗口类名黑名单 ----------
# Alt+Tab 成对闪现、锁屏过渡、输入法候选、任务栏/桌面壳层等
NOISE_CLASS = {
    "ForegroundStaging",        # Alt+Tab 时成对闪现，rect 全 0，停留 0.1s
    "LockScreenController",     # 锁屏
    "UnlockingWindow",          # 解锁过渡
    "Windows.UI.Input.InputSite",
    "IME",                      # 输入法候选窗
    "SysShadow",
    "ToolTip",
    "ReBarWindow32",
    "WorkerW",
    "Progman",                  # Program Manager（桌面）
    "DV2ControlHost",
    "XamlExplorerHost",         # 部分 UWP 壳（无内容）
}

# ---------- 进程名 → 仅保留特定类名的白名单守卫 ----------
# explorer.exe 只保留 CabinetWClass（资源管理器）；桌面(Progman)、任务栏不算应用
PROC_CLASS_GUARD = {
    "explorer.exe": {"CabinetWClass"},
}

# ---------- UWP 宿主 ----------
UWP_HOST_PROC = "ApplicationFrameHost.exe"

# ---------- IM 隐私默认规则（F9）----------
# 默认只记时长、不记窗口标题（标题可能含联系人名）
IM_PROC = {
    "WeChat.exe", "Weixin.exe",
    "Feishu.exe", "Lark.exe",
    "QQ.exe",
    "DingTalk.exe",
}

# ---------- 容量优化 ----------
SNAPSHOT_MIN_AREA_RATIO = 0.01  # 面积 < 屏幕 1% 的窗口不算可见应用（T6 三重判据之一）

# ---------- 内容级抓取（抖音等客户端，MVP 抖音内容级补充）----------
DOUYIN_PROC = "douyin.exe"
CONTENT_FETCH_INTERVAL_MS = 5000  # 抖音前台时内容抓取的节流间隔（5s 采样：UIA 全树遍历开销较大，2s 会使 CPU 达 ~6.6%）

# ---------- 手动编辑（仪表盘改分类）----------
# 可选分类 + 默认生产力映射（用户改分类时按此自动带出 productivity）
CATEGORY_OPTIONS = ["工作", "浏览网页", "娱乐", "社交通讯", "系统", "未分类"]
CATEGORY_PRODUCTIVITY = {
    "工作": "productive",
    "浏览网页": "neutral",
    "娱乐": "distracting",
    "社交通讯": "neutral",
    "系统": "neutral",
    "未分类": "neutral",
}

# 默认分类规则种子（classifier 首次运行时写入；可被用户编辑覆盖）
DEFAULT_CATEGORY_RULES = [
    # match_field, pattern, category, sub_category, productivity, priority
    ("process_name", r"(?i)WeChat|Weixin|Feishu|Lark|QQ|DingTalk", "社交通讯", "IM", "neutral", 10),
    ("process_name", r"(?i)chrome|msedge|firefox|brave", "浏览网页", "浏览器", "neutral", 5),
    ("process_name", r"(?i)Code|idea64|pycharm|studio64|devenv", "工作", "开发", "productive", 5),
    ("process_name", r"(?i)obsidian|notion|typora|word|wps|excel", "工作", "文档", "productive", 5),
    ("process_name", r"(?i)douyin|bilibili", "娱乐", "视频", "distracting", 5),
    ("process_name", r"(?i)steam|EpicGamesLauncher|Unity|wegame", "娱乐", "游戏", "distracting", 5),
    ("process_name", r"(?i)Spotify|NetEaseMusic|cloudmusic", "娱乐", "音乐", "neutral", 5),
    ("process_name", r"(?i)explorer", "系统", "文件管理", "neutral", 1),
]
