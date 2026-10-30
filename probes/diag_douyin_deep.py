# -*- coding: utf-8 -*-
"""深度诊断：展开抖音根窗口下每个子控件的层级，确认 accessibility 树是否为空。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32gui
import uiautomation as uia

uia.uiautomation.TIME_OUT = 2.0


def dump(ctrl, depth, max_depth, indent=""):
    try:
        name = (ctrl.Name or "").strip()
        cty = ctrl.ControlTypeName
        cls = str(ctrl.ClassName)[:34]
    except Exception as e:
        print(f"{indent}<读取失败 {e}>", flush=True)
        return 0
    print(f"{indent}{cty:<18} class={cls:<35} name={name[:50]!r}", flush=True)
    if depth >= max_depth:
        return 1
    try:
        chs = ctrl.GetChildren()
    except Exception as e:
        print(f"{indent}  <GetChildren 失败 {e}>", flush=True)
        return 1
    n = 1
    for ch in chs:
        n += dump(ch, depth + 1, max_depth, indent + "  ")
    return n


def main():
    hwnd = int(sys.argv[1]) if len(sys.argv) > 1 else win32gui.GetForegroundWindow()
    print(f"hwnd={hwnd} title={win32gui.GetWindowText(hwnd)!r} "
          f"fg={win32gui.GetForegroundWindow()}", flush=True)
    ctrl = uia.ControlFromHandle(hwnd)
    print(f"root: {ctrl.Name!r} {ctrl.ControlTypeName} {ctrl.ClassName!r}", flush=True)
    chs = ctrl.GetChildren()
    print(f"children={len(chs)}\n", flush=True)
    total = 0
    for i, ch in enumerate(chs):
        print(f"===== 子控件 {i} =====", flush=True)
        total += dump(ch, 0, 6)
    print(f"\n总节点(最多6层)={total}", flush=True)

    # 额外尝试：从根逐层 GetFirstChildControl
    print("\n===== FirstChild 链 =====", flush=True)
    cur = ctrl
    for d in range(8):
        try:
            cur = cur.GetFirstChildControl()
        except Exception as e:
            print(f"  depth {d} err {e}", flush=True)
            break
        if cur is None:
            print(f"  depth {d} -> None", flush=True)
            break
        try:
            print(f"  depth {d}: {cur.ControlTypeName} class={str(cur.ClassName)[:30]!r} "
                  f"name={str(cur.Name)[:40]!r}", flush=True)
        except Exception as e:
            print(f"  depth {d} read err {e}", flush=True)


if __name__ == "__main__":
    main()
