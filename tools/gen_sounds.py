# -*- coding: utf-8 -*-
"""生成番茄钟结束提示音（标准库，无外部依赖）。

设计目标：轻柔、不刺耳、不打断。
- 10ms 淡入，去掉起音的“啪”
- 指数衰减长尾，像风铃而不是电子音
- 正弦基频 + 少量二/三次谐波，音色偏暖
- 整体峰值压到约 -20 dBFS

运行：python tools/gen_sounds.py
产物：assets/sounds/focus_done.wav / break_done.wav
"""
import math
import os
import struct
import wave

SR = 44100
AMP = 0.10                     # 约 -20 dBFS
TIMBRE = ((1, 1.0), (2, 0.16), (3, 0.05))


def _tone(freq, dur, amp=AMP, decay=3.0):
    n = int(SR * dur)
    out = []
    for i in range(n):
        t = i / SR
        # 淡入 + 指数衰减；最后 40ms 线性收尾，避免截断爆音
        env = (1.0 - math.exp(-t / 0.010)) * math.exp(-decay * t)
        if t > dur - 0.04:
            env *= max(0.0, (dur - t) / 0.04)
        s = sum(w * math.sin(2 * math.pi * freq * h * t)
                for h, w in TIMBRE)
        out.append(env * s * amp)
    return out


def _mix(total_dur, parts):
    n = int(SR * total_dur)
    buf = [0.0] * n
    for start, samples in parts:
        off = int(start * SR)
        for i, v in enumerate(samples):
            if off + i < n:
                buf[off + i] += v
    peak = max(1e-9, max(abs(v) for v in buf))
    if peak > AMP:
        buf = [v * AMP / peak for v in buf]
    return buf


def _write(path, buf):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
            for v in buf))


def main():
    base = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "assets", "sounds")

    # 完成：C5 → E5 → G5，三音上行琶音
    parts = [(i * 0.18, _tone(f, 0.95, decay=3.4))
             for i, f in enumerate((523.25, 659.25, 783.99))]
    focus = _mix(0.18 * 2 + 0.95, parts)
    _write(os.path.join(base, "focus_done.wav"), focus)

    # 休息结束：G5 单音，更短更轻
    brk = _tone(783.99, 0.62, amp=0.085, decay=4.2)
    _write(os.path.join(base, "break_done.wav"), brk)

    print("focus_done.wav %.2fs  break_done.wav %.2fs"
          % (len(focus) / SR, len(brk) / SR))


if __name__ == "__main__":
    main()
