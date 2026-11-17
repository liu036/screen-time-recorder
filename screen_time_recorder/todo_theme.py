# -*- coding: utf-8 -*-
"""待办窗与番茄面板共用的配色 / 字体。

从 todo_window 抽出来，避免 pomodoro_panel 反向 import 主窗口造成循环依赖。
色值与仪表盘同一套 palm 语义：纸白底、墨色字、金色强调、砖红警示。
"""

# 背景与文字
BG = "#F5F2EA"
CARD = "#FFFDF8"
INK = "#2B2B28"
MUTED = "#8A8578"
FAINT = "#E2DCCC"
LINE = "#EAE4D5"
CHK_BORDER = "#C9C2B0"

# 语义色
ACCENT = "#D4A017"
ACCENT_DARK = "#B5650F"
DANGER = "#C0553B"
DONE = "#A9A395"

# 优先级：低 / 中 / 高（高 = 红色）
PRIO_COLORS = ("#D9D3C4", "#43593B", "#C0553B")

# 字体（元组给 Tk，Cache 用 tkfont.Font 另行创建）
F_TITLE = ("Microsoft YaHei UI", 10)
F_TITLE_HI = ("Microsoft YaHei UI", 10, "bold")
F_SMALL = ("Microsoft YaHei UI", 8)
F_BODY = ("Microsoft YaHei UI", 9)
