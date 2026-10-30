import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screen_time_recorder import tray
import win32gui, win32con

app = tray.TrayApp()
t = threading.Thread(target=app.run, daemon=True)
t.start()
print("[test] run() 已启动，等待 3s ...")
time.sleep(3)
hwnd = app.hwnd
print("[test] hwnd =", hwnd, "（None 表示无桌面会话，会走降级常驻循环）")
if hwnd:
    win32gui.PostMessage(hwnd, win32con.WM_DESTROY, 0, 0)
    print("[test] 已发送 WM_DESTROY，等待退出 ...")
t.join(timeout=5)
print("[test] 线程仍存活（降级循环/未退出）:", t.is_alive())
print("[test] 结束。无 traceback 即说明原生托盘代码路径不崩溃。")
