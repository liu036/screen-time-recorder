# -*- coding: utf-8 -*-
"""MVP-8 资源占用冒烟测试：运行 daemon 12s，采样自身进程 RSS/CPU%。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psutil
from screen_time_recorder.daemon import Daemon

proc = psutil.Process(os.getpid())
d = Daemon()
d.start()

rss_samples = []
cpu_samples = []
t0 = time.time()
print("[resource_test] 开始采样 12s ...")
while time.time() - t0 < 12:
    rss_samples.append(proc.memory_info().rss / 1024 / 1024)  # MB
    cpu_samples.append(proc.cpu_percent(interval=1.0))

d.close()
rss_samples.sort()
print(f"[resource_test] RSS  MB  min={rss_samples[0]:.1f}  "
      f"avg={sum(rss_samples)/len(rss_samples):.1f}  "
      f"max={rss_samples[-1]:.1f}  (目标 <80)")
print(f"[resource_test] CPU%  min={min(cpu_samples):.2f}  "
      f"avg={sum(cpu_samples)/len(cpu_samples):.2f}  "
      f"max={max(cpu_samples):.2f}  (目标 <0.5)")
print("[resource_test] 完成，已落盘关闭。")
