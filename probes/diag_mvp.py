# -*- coding: utf-8 -*-
"""MVP 诊断：逐步跑采集链路，暴露静默异常。"""
import sys
import io
import time
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("=== step1 import ===")
from screen_time_recorder.storage import Storage
from screen_time_recorder.collector import Collector, get_idle_ms
from screen_time_recorder.snapshot import SnapshotCollector
import ctypes

print("=== step2 storage ===")
st = Storage()
print("events:", st.count_events(), "snapshots:", st.count_snapshots())

print("=== step3 collector start ===")
c = Collector(st)
try:
    c.start()
    print("collector.start ok")
except Exception as e:
    print("collector.start ERR:", repr(e))
    traceback.print_exc()

print("=== step4 manual foreground ===")
fg = int(ctypes.windll.user32.GetForegroundWindow())
print("fg hwnd:", fg)
try:
    c._handle_foreground(fg, "foreground")
    print("handle_foreground ok")
except Exception as e:
    print("handle_foreground ERR:", repr(e))
    traceback.print_exc()

print("=== step5 title ===")
try:
    c._handle_title(fg)
    print("handle_title ok")
except Exception as e:
    print("handle_title ERR:", repr(e))
    traceback.print_exc()

print("=== step6 snapshot ===")
snap = SnapshotCollector(st)
try:
    snap._collect()
    print("snapshot ok")
except Exception as e:
    print("snapshot ERR:", repr(e))
    traceback.print_exc()

time.sleep(2)
print("=== step7 counts ===")
print("events:", st.count_events(), "snapshots:", st.count_snapshots())
st.close()
