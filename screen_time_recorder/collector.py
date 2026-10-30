# -*- coding: utf-8 -*-
"""采集层（MVP-3）。

落实《3-技术调研报告》的关键结论：
- 事件驱动必须 ctypes 直调 SetWinEventHook，且「注册钩子的线程 = 跑消息循环的线程」
  （OUTOFCONTEXT 回调在调用线程执行，该线程必须有消息循环）
- 轮询兜底 250ms 补漏（事件驱动为主）
- 噪音过滤：进程名 + 类名双重黑名单 + 进程类名守卫（T5）
- UWP：AFH 窗口 → 子窗口 pid → 真实进程（T5）
- IM 隐私：微信/飞书等默认只记时长、标题留空（F9）
- 锁屏/解锁：LockApp.exe / hwnd==0 + 2s debounce（T3）
- AFK：GetLastInputInfo（T3）
- <2s 碎片在 Activity 派生阶段丢弃，此处只落原始事件（先过滤噪音再套阈值）
"""
import ctypes
import threading
import time
from ctypes import wintypes

import win32con
import win32gui
import win32process
import psutil

from . import config
from . import douyin_content

# ---------- Windows API ----------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
PM_REMOVE = 0x0001

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
)
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.UnhookWinEvent.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND,
    wintypes.UINT, wintypes.UINT, wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def get_idle_ms():
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if user32.GetLastInputInfo(ctypes.byref(li)):
        now = kernel32.GetTickCount()
        return int((now - li.dwTime) % (2 ** 32))  # 处理 49.7 天回绕
    return 0


# ---------- 应用名归一化 ----------
APP_NAME_MAP = {
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Mozilla Firefox",
    "Code.exe": "Visual Studio Code",
    "idea64.exe": "IntelliJ IDEA",
    "pycharm64.exe": "PyCharm",
    "devenv.exe": "Visual Studio",
    "Obsidian.exe": "Obsidian",
    "notion.exe": "Notion",
    "typora.exe": "Typora",
    "WINWORD.EXE": "Microsoft Word",
    "EXCEL.EXE": "Microsoft Excel",
    "wps.exe": "WPS Office",
    "WeChat.exe": "微信",
    "Weixin.exe": "微信",
    "Feishu.exe": "飞书",
    "Lark.exe": "Lark",
    "QQ.exe": "QQ",
    "DingTalk.exe": "钉钉",
    "douyin.exe": "抖音",
    "bilibili.exe": "哔哩哔哩",
    "steam.exe": "Steam",
    "Spotify.exe": "Spotify",
    "cloudmusic.exe": "网易云音乐",
    "explorer.exe": "文件资源管理器",
}


def normalize_app_name(process_name, exe_path=None):
    if process_name in APP_NAME_MAP:
        return APP_NAME_MAP[process_name]
    base = process_name[:-4] if process_name.lower().endswith(".exe") else process_name
    return base or "unknown"


def resolve_real_pid(hwnd, host_pid):
    """UWP：ApplicationFrameHost 宿主 → 子窗口真实 pid（T5）。

    采集与快照共用，避免两处逻辑不一致导致 AFH 被记成一个"应用"。
    """
    found = []

    def enum_child(h, _):
        try:
            _, cp = win32process.GetWindowThreadProcessId(h)
            if cp != host_pid:
                found.append(cp)
                return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, enum_child, None)
    except Exception:
        pass
    return found[0] if found else host_pid


# ---------- 噪音标题黑名单（T5 实测：任务切换/贴靠助手等）----------
NOISE_TITLE_PREFIX = (
    "任务切换", "贴靠助手", "Windows 输入体验", "UnlockingWindow",
)


class Collector:
    def __init__(self, storage):
        self.storage = storage
        self.stop = threading.Event()
        self._state_lock = threading.Lock()
        self.last_fg_hwnd = None
        self.last_fg_ts = 0
        self.lock_state = 0  # 0 未知, 1 解锁, 2 锁屏
        self._last_lock_event_ts = 0
        self._proc_ref = None  # 保持回调引用，防 GC
        self.hook_fg = None
        self.hook_name = None
        # 当前前台追踪（供内容级抓取线程判断"是否抖音前台"）
        self._fg_lock = threading.Lock()
        self._fg_proc = None
        self._fg_hwnd = None
        self._content_fetcher = None
        self._last_title_key = None  # (hwnd, title) 去重，防同标题重复落库

    # ---------- 窗口信息收集 ----------
    def _resolve_real_pid(self, hwnd, host_pid):
        return resolve_real_pid(hwnd, host_pid)

    def get_window_info(self, hwnd):
        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        try:
            title = win32gui.GetWindowText(hwnd) or ""
            cls = win32gui.GetClassName(hwnd) or ""
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid).name()
            if proc.lower() == config.UWP_HOST_PROC.lower():
                real_pid = self._resolve_real_pid(hwnd, pid)
                proc = psutil.Process(real_pid).name()
                exe = psutil.Process(real_pid).exe()
            else:
                exe = psutil.Process(pid).exe()
            iconic = bool(win32gui.IsIconic(hwnd))
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            rect = (l, t, r - l, b - t)
        except Exception:
            return None
        return {
            "hwnd": int(hwnd), "title": title, "cls": cls,
            "process_name": proc, "exe_path": exe,
            "iconic": iconic, "rect": rect,
        }

    # ---------- 过滤 ----------
    def _is_noise(self, info):
        proc = info["process_name"]
        cls = info["cls"]
        title = info["title"]
        if proc in config.NOISE_PROC:
            return True
        if any(k in proc.lower() for k in config.NOISE_PROC_KEYWORDS):
            return True
        if cls in config.NOISE_CLASS:
            return True
        if proc in config.PROC_CLASS_GUARD and cls not in config.PROC_CLASS_GUARD[proc]:
            return True
        if title and title.startswith(NOISE_TITLE_PREFIX):
            return True
        return False

    # ---------- 事件处理 ----------
    def _emit(self, info, event_type):
        """写入一条已过滤的前台/标题事件"""
        # 同窗口同标题的重复 namechange 不重复落库（防事件量膨胀）
        with self._state_lock:
            key = (info["hwnd"], info["title"])
            if event_type == "title_change" and key == self._last_title_key:
                return
            self._last_title_key = key
        proc = info["process_name"]
        app_name = normalize_app_name(proc, info["exe_path"])
        app_id = self.storage.ensure_app(proc, info["exe_path"], app_name, info["cls"])
        # IM 隐私：标题留空（F9）
        title = "" if proc in config.IM_PROC else info["title"]
        idle = get_idle_ms()
        # 记录当前前台，供内容级抓取线程判断"是否抖音前台"
        with self._fg_lock:
            self._fg_proc = proc
            self._fg_hwnd = info["hwnd"]
        self.storage.buffer_event(
            ts=int(time.time() * 1000),
            event_type=event_type,
            hwnd=info["hwnd"],
            app_id=app_id,
            window_title=title,
            rect=",".join(map(str, info["rect"])),
            cls=info["cls"],
            idle_ms=idle,
            is_afk=1 if idle > config.AFK_THRESHOLD_MS else 0,
            raw="",
        )

    def _handle_foreground(self, hwnd, event_type="foreground"):
        now = int(time.time() * 1000)
        # 锁屏检测：hwnd==0 或 LockApp.exe（2s debounce）
        if not hwnd or not win32gui.IsWindow(hwnd):
            with self._state_lock:
                if self.lock_state != 2 and now - self._last_lock_event_ts > 2000:
                    self.lock_state = 2
                    self._last_lock_event_ts = now
                    self.storage.buffer_event(
                        ts=now, event_type="lock", hwnd=0, app_id=0,
                        window_title="", rect="", cls="",
                        idle_ms=get_idle_ms(), is_afk=1, raw="")
            return
        info = self.get_window_info(hwnd)
        if not info:
            return
        proc = info["process_name"]
        if proc.lower() == "lockapp.exe":
            with self._state_lock:
                if self.lock_state != 2 and now - self._last_lock_event_ts > 2000:
                    self.lock_state = 2
                    self._last_lock_event_ts = now
                    self.storage.buffer_event(
                        ts=now, event_type="lock", hwnd=info["hwnd"], app_id=0,
                        window_title="", rect="", cls=info["cls"],
                        idle_ms=get_idle_ms(), is_afk=1, raw="")
            return
        # 解锁：从锁屏恢复正常窗口
        if self.lock_state == 2:
            with self._state_lock:
                self.lock_state = 1
                self._last_lock_event_ts = now
                self.storage.buffer_event(
                    ts=now, event_type="unlock", hwnd=info["hwnd"], app_id=0,
                    window_title="", rect="", cls="",
                    idle_ms=get_idle_ms(), is_afk=0, raw="")
        if self._is_noise(info):
            # 噪音窗口也更新前台追踪，避免内容抓取线程误以为上一个应用仍在前台
            with self._fg_lock:
                self._fg_proc = proc
                self._fg_hwnd = info["hwnd"]
            return
        with self._state_lock:
            if info["hwnd"] == self.last_fg_hwnd:
                return  # 同一窗口重复，不写
            self.last_fg_hwnd = info["hwnd"]
            self.last_fg_ts = now
        self._emit(info, event_type)

    def _handle_title(self, hwnd):
        info = self.get_window_info(hwnd)
        if not info or self._is_noise(info):
            return
        with self._state_lock:
            if info["hwnd"] != self.last_fg_hwnd:
                return  # 只记录前台窗口的标题变化（Q11）
        self._emit(info, "title_change")

    # ---------- ctypes 回调 ----------
    def _on_event(self, h_hook, event, hwnd, id_object, id_child, thread, t):
        if id_object != 0 or id_child != 0:
            return
        try:
            if event == EVENT_SYSTEM_FOREGROUND:
                self._handle_foreground(int(hwnd), "foreground")
            elif event == EVENT_OBJECT_NAMECHANGE:
                self._handle_title(int(hwnd))
        except Exception as e:
            print(f"[collector] callback error: {e}")

    # ---------- 线程 ----------
    def _hook_thread(self):
        self._proc_ref = WINEVENTPROC(self._on_event)
        self.hook_fg = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
            self._proc_ref, 0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        self.hook_name = user32.SetWinEventHook(
            EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE, None,
            self._proc_ref, 0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        msg = wintypes.MSG()
        while not self.stop.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)
        if self.hook_fg:
            user32.UnhookWinEvent(self.hook_fg)
        if self.hook_name:
            user32.UnhookWinEvent(self.hook_name)

    def _poll_thread(self):
        """250ms 轮询兜底：事件驱动漏掉的瞬态切换补回来"""
        while not self.stop.is_set():
            try:
                fg = int(user32.GetForegroundWindow())
                self._handle_foreground(fg, "foreground")
            except Exception:
                pass
            time.sleep(config.HOOK_POLL_MS / 1000.0)

    def start(self):
        self.stop.clear()  # 支持暂停后恢复（stop_now 置位后需清除才能重启）
        t = threading.Thread(target=self._hook_thread, daemon=True)
        t.start()
        p = threading.Thread(target=self._poll_thread, daemon=True)
        p.start()
        # 抖音内容级抓取线程（仅抖音前台时按节流间隔抓）
        self._content_fetcher = ContentFetcher(self)
        self._content_fetcher.start()
        # 立即捕获一次当前前台
        try:
            self._handle_foreground(int(user32.GetForegroundWindow()), "foreground")
        except Exception:
            pass

    def stop_now(self):
        self.stop.set()
        if self._content_fetcher:
            self._content_fetcher.stop.set()


class ContentFetcher(threading.Thread):
    """抖音内容级抓取线程。

    仅在抖音前台时，按 CONTENT_FETCH_INTERVAL_MS 节流间隔，用 UIA 抓取当前视频
    标题/作者。抓到的新内容写入 'content' 事件（带 content_title），供统计按内容聚合。

    设计约束：
    - 不抢前台（抖音本就是前台时才抓，绝调 SetForegroundWindow）
    - 抓取带整体超时、失败静默跳过，不向外抛异常、不阻塞采集主线程
    """

    def __init__(self, collector):
        super().__init__(daemon=True)
        self.c = collector
        self.stop = threading.Event()
        self._last_content = None
        self._douyin_app_id = None

    def run(self):
        while not self.stop.is_set():
            time.sleep(config.CONTENT_FETCH_INTERVAL_MS / 1000.0)
            if self.stop.is_set():
                break
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self):
        with self.c._fg_lock:
            proc = self.c._fg_proc
            hwnd = self.c._fg_hwnd
        if proc != config.DOUYIN_PROC or not hwnd:
            return
        content = douyin_content.fetch_douyin_content(hwnd)
        if not content:
            # 抓不到内容时尝试自愈：抖音重启后读屏标签可能被重置为关闭
            douyin_content.ensure_reading_tags(hwnd)
            return
        if content == self._last_content:
            return
        self._last_content = content
        if self._douyin_app_id is None:
            self._douyin_app_id = self.c.storage.ensure_app(
                config.DOUYIN_PROC, None, "抖音", "Chrome_WidgetWin_1")
        self.c.storage.buffer_event(
            ts=int(time.time() * 1000),
            event_type="content",
            hwnd=hwnd,
            app_id=self._douyin_app_id,
            window_title="",
            rect="",
            cls="",
            idle_ms=get_idle_ms(),
            is_afk=0,
            raw="",
            content_title=content,
        )
