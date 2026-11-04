# -*- coding: utf-8 -*-
"""屏幕时间记录器 MVP 入口。

用法:
  python main.py start    启动常驻采集（MVP 闭环）
  python main.py status   查看采集状态（事件/快照条数）
"""
import os
import sys
import io
import ctypes
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 注意：这里不要在顶层 import daemon/storage/cli。
# `main.py todo` 只需要 tkinter，若顶层导入采集栈，会把 uiautomation、
# win32gui、sqlite3 全部加载进待办窗口进程，白白多占 ~55MB。

# 命名互斥体句柄必须保持模块级引用，否则被 GC 关闭后单实例保护失效
_mutex_handle = None


def _acquire_single_instance():
    """用 Windows 命名互斥体保证单实例，避免双击两次导致重复采集。"""
    global _mutex_handle
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        _mutex_handle = kernel32.CreateMutexW(
            None, False, "ScreenTimeRecorder_SingleInstance")
        if not _mutex_handle:
            return True  # 互斥体创建失败则放行，不阻断正常使用
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _boot_log_and_box():
    """在导入 tray 之前就重定向 stdout/stderr 到日志，并提供一个错误弹窗。
    避免 pythonw（无控制台）下任何早期异常都被静默吞掉。"""
    try:
        from screen_time_recorder import config
        log_dir = os.path.dirname(config.DB_PATH)
        os.makedirs(log_dir, exist_ok=True)
        f = open(os.path.join(log_dir, "tray.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
    except Exception:
        pass


def main():
    if len(sys.argv) < 2:
        print("用法: python main.py "
              "[start|status|today|yesterday|apps|export|delete|todo|dashboard|autostart]")
        return
    cmd = sys.argv[1]
    if cmd == "start":
        if not _acquire_single_instance():
            try:
                ctypes.windll.user32.MessageBoxW(
                    0, "屏幕时间记录器已在运行，请勿重复启动。\n\n"
                       "如需重启，请先右键托盘图标选择「退出」。",
                    "屏幕时间记录器", 0x40)
            except Exception:
                pass
            return
        _boot_log_and_box()
        try:
            from screen_time_recorder import tray
            tray.TrayApp().run()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[main] 启动失败: {e}\n{tb}")
            try:
                ctypes.windll.user32.MessageBoxW(
                    0, f"启动失败：{e}\n\n{tb}", "屏幕时间记录器", 0x10)
            except Exception:
                pass
            # 即便托盘失败，也尽量保持采集
            try:
                from screen_time_recorder.daemon import Daemon
                Daemon().run()
            except Exception:
                pass
    elif cmd == "dashboard":
        from screen_time_recorder.web import run_standalone
        run_standalone()
    elif cmd == "todo":
        # 待办便笺窗（独立进程；--focus 时启动后聚焦输入框）
        from screen_time_recorder.todo_window import main as todo_main
        todo_main()
    elif cmd in ("status", "today", "yesterday", "apps",
                 "export", "delete"):
        # 只有这些命令需要存储层，按需导入（避免 todo/dashboard 付加载代价）
        from screen_time_recorder.storage import Storage
        from screen_time_recorder import cli

        if cmd == "status":
            st = Storage()
            print(f"事件条数: {st.count_events()}")
            print(f"快照条数: {st.count_snapshots()}")
            st.close()
            return
        if cmd == "apps":
            st = Storage()
            cli.cmd_apps(st)
            st.close()
            return

        if cmd == "today":
            day = datetime.now().strftime("%Y-%m-%d")
        elif cmd == "yesterday":
            day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            day = (sys.argv[2] if len(sys.argv) > 2
                   else datetime.now().strftime("%Y-%m-%d"))
        fmt = sys.argv[3] if len(sys.argv) > 3 else "csv"
        st = Storage()
        try:
            if cmd == "export":
                cli.cmd_export(st, day, fmt, None)
            elif cmd == "delete":
                cli.cmd_delete(st, day)
            else:
                cli.cmd_view(st, day)
        finally:
            st.close()
    elif cmd == "autostart":
        from screen_time_recorder import tray
        tray.install_autostart(True)
    elif cmd == "noautostart":
        from screen_time_recorder import tray
        tray.install_autostart(False)
    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
