# -*- coding: utf-8 -*-
"""系统托盘（MVP-6）：用 pywin32 原生实现，去掉 pystray 依赖。

为什么不用 pystray：0.19.5 是 2022 年的老库，在 Python 3.13 上建图标不稳定，
且失败时是静默的（pythonw 无控制台），排查困难。pywin32 是本项目的硬依赖，
对 3.13 支持良好，Windows 托盘用原生 API 最稳。
"""
import os
import sys
import io
import time
import tempfile
import ctypes
import subprocess
from datetime import datetime

import win32api
import win32con
import win32gui
import pywintypes

from PIL import Image, ImageDraw

from .daemon import Daemon
from . import cli
from . import config
from .reminder import ReminderService

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NIN_BALLOONUSERCLICK = 0x0405  # 点击气泡正文


# ---------- 气泡通知（原生 NOTIFYICONDATAW，避免 pywin32 版本差异）----------
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_ulong),
        ("dwStateMask", ctypes.c_ulong),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeout", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_ulong),
        ("guidItem", _GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


def _balloon(hwnd, hicon, tip, title, body):
    """弹出托盘气泡。返回 False 表示系统未接受（可能被专注助手拦截）。"""
    NIF_INFO, NIF_TIP, NIIF_INFO, NIM_MODIFY = 0x10, 0x04, 0x01, 0x01
    try:
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 0
        nid.uFlags = NIF_INFO | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = hicon or 0
        nid.szTip = (tip or "")[:127]
        nid.szInfo = (body or "")[:255]
        nid.szInfoTitle = (title or "")[:63]
        nid.dwInfoFlags = NIIF_INFO
        nid.uTimeout = 10000
        shell32 = ctypes.windll.shell32
        shell32.Shell_NotifyIconW.argtypes = [
            ctypes.c_ulong, ctypes.POINTER(_NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = ctypes.c_bool
        return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
    except Exception as e:
        print(f"[tray] 气泡通知失败: {e}")
        return False


def _setup_logging():
    """GUI 模式下 stdout/stderr 无控制台，重定向到日志文件，避免 print 抛异常。"""
    try:
        log_dir = os.path.dirname(config.DB_PATH)
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "tray.log")
        f = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
        return log_path
    except Exception:
        return None


def _show_message_box(title, text, error=False):
    try:
        style = 0x10 if error else 0x40  # MB_ICON_ERROR / MB_ICON_INFORMATION
        ctypes.windll.user32.MessageBoxW(0, text, title, style)
    except Exception:
        pass


def build_ico():
    """生成蓝色圆形图标 .ico 到临时文件，返回路径。"""
    img = Image.new("RGBA", (32, 32), (30, 144, 255, 255))
    d = ImageDraw.Draw(img)
    d.ellipse([5, 5, 27, 27], fill=(255, 255, 255, 255))
    d.ellipse([12, 12, 20, 20], fill=(30, 144, 255, 255))
    fd, path = tempfile.mkstemp(suffix=".ico", prefix="str_")
    os.close(fd)
    img.save(path, format="ICO")
    return path


WM_TRAYICON = win32con.WM_USER + 1
MSG_TASKBAR_CREATED = win32gui.RegisterWindowMessage("TaskbarCreated")


class TrayApp:
    def __init__(self):
        self.daemon = Daemon()
        self.paused = False
        self.hwnd = None
        self.hicon = None
        self._wndclass = None
        self._ico_path = build_ico()
        self._tip = "屏幕时间记录器 · 待办"
        self._web_url = None
        self.reminder = None

    # ---- 托盘图标 ----
    def _add_icon(self):
        if not self.hwnd or not self.hicon:
            return
        nid = (self.hwnd, 0,
               win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
               WM_TRAYICON, self.hicon, self._tip)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        except Exception as e:
            print(f"[tray] NIM_ADD 失败（无桌面会话？）: {e}")
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception:
            pass

    def _remove_icon(self):
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception:
            pass

    # ---- 隐藏窗口 + 消息循环 ----
    def _create_window(self):
        hinst = win32api.GetModuleHandle(None)
        class_name = "ScreenTimeRecorderTray"
        self._wndclass = win32gui.WNDCLASS()
        self._wndclass.hInstance = hinst
        self._wndclass.lpszClassName = class_name
        self._wndclass.lpfnWndProc = self._wndproc
        self._wndclass.style = win32con.CS_VREDRAW | win32con.CS_HREDRAW
        self._wndclass.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        self._wndclass.hbrBackground = win32con.COLOR_WINDOW + 1
        try:
            win32gui.RegisterClass(self._wndclass)
        except win32gui.error:
            pass  # 同一进程内已注册
        self.hwnd = win32gui.CreateWindow(
            class_name, self._tip, 0,
            0, 0, 0, 0, 0, 0, hinst, None)
        win32gui.UpdateWindow(self.hwnd)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == MSG_TASKBAR_CREATED:
            self._add_icon()
            return 0
        if msg == WM_TRAYICON:
            if lparam == NIN_BALLOONUSERCLICK:
                self.open_todo_window()
                return 0
            if lparam in (win32con.WM_RBUTTONUP, win32con.WM_RBUTTONDOWN):
                self._show_menu()
                return 0
            if lparam in (win32con.WM_LBUTTONDBLCLK, win32con.WM_LBUTTONUP):
                self.on_today()
                return 0
            return 0
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # ---- 右键菜单 ----
    def _show_menu(self):
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, 1, "今日统计")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 2, "打开仪表盘")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 3, "待办清单")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 4, "快速添加待办…")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(
            menu, win32con.MF_STRING, 5,
            "暂停采集" if not self.paused else "继续采集")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 6, "开机自启")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 7, "退出")
        pos = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(self.hwnd)
        cmd = win32gui.TrackPopupMenu(
            menu,
            win32con.TPM_RETURNCMD | win32con.TPM_NONOTIFY | win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON,
            pos[0], pos[1], 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)
        if cmd == 1:
            self.on_today()
        elif cmd == 2:
            self.on_dashboard()
        elif cmd == 3:
            self.open_todo_window(focus=False)
        elif cmd == 4:
            self.open_todo_window(focus=True)
        elif cmd == 5:
            self.on_pause()
        elif cmd == 6:
            self.on_autostart()
        elif cmd == 7:
            self.on_quit()

    # ---- 菜单动作 ----
    def _today_text(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            cli.cmd_view(self.daemon.storage, datetime.now().strftime("%Y-%m-%d"))
        except Exception as e:
            buf.write(f"统计失败: {e}\n")
        finally:
            sys.stdout = old
        return buf.getvalue().strip() or "（暂无数据）"

    def on_today(self):
        _show_message_box("屏幕时间记录器 · 今日统计", self._today_text())

    def on_dashboard(self):
        url = self._web_url or "http://127.0.0.1:8765/"
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            _show_message_box("屏幕时间记录器", f"打开仪表盘失败：{e}", error=True)

    # ---- 待办 ----
    def open_todo_window(self, focus=False):
        """拉起待办窗（独立进程）。已存在时窗口自身会激活自己。"""
        script = os.path.join(BASE_DIR, "main.py")
        exe = sys.executable
        # 确保用 GUI 子系统，避免弹控制台
        if exe.lower().endswith("python.exe"):
            cand = exe[:-len("python.exe")] + "pythonw.exe"
            if os.path.exists(cand):
                exe = cand
        args = [exe, script, "todo"]
        if focus:
            args.append("--focus")
        try:
            subprocess.Popen(args, cwd=BASE_DIR,
                             creationflags=0x00000008)  # DETACHED_PROCESS
        except Exception as e:
            _show_message_box("屏幕时间记录器",
                              f"打开待办窗口失败：{e}", error=True)

    def notify(self, title, body, todo_ids=None):
        """ReminderService 的通知回调：托盘气泡。"""
        ok = _balloon(self.hwnd, self.hicon, self._tip, title, body)
        if not ok:
            print(f"[tray] 气泡未被系统接受：{title} / {body!r}")
        return ok

    def _availability(self):
        """免打扰判定：锁屏或长时间离开时先不弹，恢复后会补。"""
        try:
            if self.daemon.collector.lock_state == 2:
                return False, "锁屏"
            from .collector import get_idle_ms
            idle = get_idle_ms()
            if idle > config.REMINDER_IDLE_LIMIT_MS:
                return False, "离开 %d 分钟" % (idle // 60000)
        except Exception:
            pass
        return True, ""

    def _update_tip(self, summary):
        n = summary.get("open", 0)
        od = summary.get("overdue", 0)
        tip = "屏幕时间记录器 · 未完成 %d" % n
        if od:
            tip += "（逾期 %d）" % od
        self._tip = tip[:127]
        try:
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_MODIFY,
                (self.hwnd, 0, win32gui.NIF_TIP, WM_TRAYICON,
                 self.hicon, self._tip))
        except Exception:
            pass

    def on_pause(self):
        if self.paused:
            self.daemon.resume()
            self.paused = False
            _show_message_box("屏幕时间记录器", "已继续采集")
        else:
            self.daemon.pause()
            self.paused = True
            _show_message_box("屏幕时间记录器", "已暂停采集")

    def on_autostart(self):
        install_autostart(True)
        _show_message_box("屏幕时间记录器", "已设置开机自启（HKCU\\Run，重启后自动启动）")

    def on_quit(self):
        self._remove_icon()
        try:
            if self.reminder:
                self.reminder.stop()
        except Exception:
            pass
        try:
            self.daemon.close()
        except Exception:
            pass
        try:
            win32gui.DestroyWindow(self.hwnd)
        except Exception:
            pass
        win32gui.PostQuitMessage(0)

    # ---- 运行 ----
    def run(self):
        _setup_logging()
        self.daemon.start()
        # 本地仪表盘（127.0.0.1:8765），随采集器常驻
        try:
            from . import web
            self._web_url = web.start_server(self.daemon.storage)
            print(f"[tray] 仪表盘: {self._web_url}")
        except Exception as e:
            print(f"[tray] 仪表盘启动失败: {e}")
        try:
            self.hicon = win32gui.LoadImage(
                0, self._ico_path, win32con.IMAGE_ICON, 0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
            self._create_window()
            self._add_icon()
            # 待办提醒（依赖已建好的 hwnd / hicon）
            try:
                self.reminder = ReminderService(
                    self.daemon.storage, self.notify,
                    availability=self._availability, badge=self._update_tip)
                self.reminder.start()
                self._update_tip(self.daemon.storage.todo_summary())
            except Exception as e:
                print(f"[tray] 提醒服务启动失败: {e}")
            print("[tray] 托盘已启动，开始常驻采集。")
            win32gui.PumpMessages()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[tray] 原生托盘启动失败: {e}\n{tb}")
            _show_message_box(
                "屏幕时间记录器启动异常",
                f"托盘图标创建失败，但采集仍在后台运行。\n\n错误: {e}\n\n详情见 tray.log",
                error=True)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        finally:
            try:
                self.daemon.close()
            except Exception:
                pass
            try:
                if self._ico_path and os.path.exists(self._ico_path):
                    os.remove(self._ico_path)
            except Exception:
                pass


def install_autostart(enable=True):
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
        if enable:
            exe = sys.executable
            # 命令行下 sys.executable 是 python.exe，开机自启会带出控制台黑窗；
            # 自动换成同目录的 pythonw.exe（GUI 子系统）。
            if exe.lower().endswith("python.exe"):
                cand = exe[:-len("python.exe")] + "pythonw.exe"
                if os.path.exists(cand):
                    exe = cand
            script = os.path.abspath(sys.argv[0])
            cmd = f'"{exe}" "{script}" start'
            winreg.SetValueEx(key, "ScreenTimeRecorder", 0, winreg.REG_SZ, cmd)
            print("[tray] 已设置开机自启")
        else:
            try:
                winreg.DeleteValue(key, "ScreenTimeRecorder")
                print("[tray] 已取消开机自启")
            except FileNotFoundError:
                pass
    except Exception as e:
        print(f"[tray] 自启设置失败: {e}")


def main():
    TrayApp().run()


if __name__ == "__main__":
    main()
