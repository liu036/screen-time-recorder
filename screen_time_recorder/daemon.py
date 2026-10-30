# -*- coding: utf-8 -*-
"""常驻守护进程（MVP 串联采集 + 快照 + 心跳）。"""
import threading
import time

from . import config
from .collector import Collector, get_idle_ms
from .snapshot import SnapshotCollector
from .storage import Storage


class Daemon:
    def __init__(self):
        self.storage = Storage()
        self.collector = Collector(self.storage)
        self.snapshot = SnapshotCollector(self.storage)
        self.stop = threading.Event()

    def _heartbeat(self):
        while not self.stop.is_set():
            time.sleep(config.HEARTBEAT_MS / 1000.0)
            idle = get_idle_ms()
            self.storage.buffer_event(
                ts=int(time.time() * 1000), event_type="heartbeat",
                hwnd=0, app_id=0, window_title="", rect="", cls="",
                idle_ms=idle,
                is_afk=1 if idle > config.AFK_THRESHOLD_MS else 0, raw="")

    def start(self):
        """非阻塞启动所有采集线程（托盘 / 测试共用）。"""
        self.collector.start()
        self.snapshot.start()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def pause(self):
        """暂停采集（托盘菜单用）。"""
        self.collector.stop_now()
        self.snapshot.stop.set()

    def resume(self):
        """恢复采集（托盘菜单用）。"""
        self.collector.start()
        self.snapshot.start()

    def run(self):
        """命令行阻塞模式（无 GUI 或测试用）。"""
        self.start()
        print("[daemon] 屏幕时间记录器已启动（MVP）。Ctrl+C 退出。")
        try:
            while not self.stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.collector.stop_now()
            self.snapshot.stop.set()
            self.storage.close()
            print("[daemon] 已停止，数据已落盘。")

    def close(self):
        self.collector.stop_now()
        self.snapshot.stop.set()
        self.storage.close()
