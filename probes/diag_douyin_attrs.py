# -*- coding: utf-8 -*-
"""探测抖音 UIA 节点的隐藏属性（AutomationId/HelpText/Value），看关闭读屏标签时能否读到内容。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32gui
import uiautomation as uia

uia.uiautomation.TIME_OUT = 2.0


def probe(ctrl, depth, max_depth):
    if depth > max_depth:
        return
    try:
        name = (ctrl.Name or "").strip()
        aid = ""
        helptext = ""
        value = ""
        try:
            aid = str(getattr(ctrl, "AutomationId", "") or "")
        except Exception:
            pass
        try:
            helptext = str(getattr(ctrl, "HelpText", "") or "")
        except Exception:
            pass
        try:
            vp = ctrl.GetValuePattern()
            if vp:
                value = str(vp.Value or "")
        except Exception:
            pass
        ass = ""
        try:
            ass = str(ctrl.GetLegacyIAccessiblePattern().Value or "")
        except Exception:
            pass
        if name or helptext or value or ass or aid:
            print(f"{'  '*depth}{ctrl.ControlTypeName:<18} "
                  f"name={name[:40]!r} aid={aid[:24]!r} help={helptext[:30]!r} "
                  f"val={value[:30]!r} legacy={ass[:30]!r}", flush=True)
    except Exception:
        return
    try:
        chs = ctrl.GetChildren()
    except Exception:
        return
    for ch in chs:
        probe(ch, depth + 1, max_depth)


def main():
    hwnd = int(sys.argv[1]) if len(sys.argv) > 1 else win32gui.GetForegroundWindow()
    ctrl = uia.ControlFromHandle(hwnd)
    print(f"hwnd={hwnd} fg={win32gui.GetForegroundWindow()} title={win32gui.GetWindowText(hwnd)!r}",
          flush=True)
    probe(ctrl, 0, 10)
    print("done", flush=True)


if __name__ == "__main__":
    main()
