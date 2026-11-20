# -*- coding: utf-8 -*-
"""结束提示音的音频参数与播放兜底。"""
import math
import os
import struct
import sys
import wave

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from screen_time_recorder import sound


def _read(name):
    path = sound.path_of(name)
    with wave.open(path, "rb") as w:
        assert w.getnchannels() == 1, "应为单声道"
        assert w.getsampwidth() == 2, "应为 16bit"
        assert w.getframerate() == 44100, "应为 44.1kHz"
        n = w.getnframes()
        raw = w.readframes(n)
    vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
    return vals


def test_sound_files():
    specs = {"focus_done": (0.8, 1.8), "break_done": (0.3, 1.0)}
    for name, (lo, hi) in specs.items():
        vals = _read(name)
        dur = len(vals) / 44100.0
        peak = max(abs(v) for v in vals) / 32767.0
        rms = math.sqrt(sum(v * v for v in vals) / len(vals)) / 32767.0
        head = max(abs(v) for v in vals[:220]) / 32767.0   # 前 5ms
        assert lo <= dur <= hi, (name, dur)
        assert 0.03 <= peak <= 0.15, "%s 峰值应轻柔：%.3f" % (name, peak)
        assert rms < peak * 0.5, "%s 不应是持续满音量" % name
        assert head < peak * 0.5, "%s 应有淡入，避免起音爆响" % name
        print("%-11s %.2fs peak=%.3f rms=%.3f 淡入=%.3f OK"
              % (name, dur, peak, rms, head))


def test_play_missing_file():
    assert sound.play("__not_exist__") is False, "缺文件应静默返回 False"


if __name__ == "__main__":
    test_sound_files()
    test_play_missing_file()
    print("all sound tests passed")
