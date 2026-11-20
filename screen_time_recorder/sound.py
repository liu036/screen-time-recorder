# -*- coding: utf-8 -*-
"""结束提示音：内置 WAV + winsound 异步播放。

为什么不用系统提示音：Win 默认音偏尖、偏“报错感”，而且随主题变化。
内置 WAV 的音色、音量、包络都是固定的，能保证“轻柔”这个要求。

为什么用 SND_ASYNC：播放发生在番茄钟的 tick 线程里，同步播放会卡住
状态更新；异步播放立即返回，最坏情况是没声卡/文件缺失时静默失败。
"""
import os

try:
    import winsound
except ImportError:          # 非 Windows（仅测试环境）
    winsound = None

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUND_DIR = os.path.join(BASE, "assets", "sounds")

_FILES = {
    "focus_done": "focus_done.wav",
    "break_done": "break_done.wav",
}


def path_of(name):
    return os.path.join(SOUND_DIR, _FILES.get(name, name + ".wav"))


def play(name):
    """播放内置提示音；失败返回 False，不抛异常。"""
    if winsound is None:
        return False
    path = path_of(name)
    if not os.path.exists(path):
        return False
    try:
        winsound.PlaySound(
            path,
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        return True
    except Exception as e:
        print("[sound] 播放失败: %s" % e)
        return False
