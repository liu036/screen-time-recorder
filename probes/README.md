# probes 说明

调试 / 验证脚本，**不属于运行时**。正式测试见 `../tests/`。

## 采集与资源

| 脚本 | 用途 |
|---|---|
| `diag_mvp.py` | 分步诊断采集链路（暴露被静默吞掉的异常） |
| `switch_test.py` | 主动切换前台窗口，验证事件捕获 |
| `resource_test.py` | 12s CPU / 内存冒烟测试 |

## 抖音内容级

| 脚本 | 用途 |
|---|---|
| `probe_douyin.py` | 最初的 UIA 可行性探测（**会抢前台**，仅调试用） |
| `check_douyin_uia.py` | 检测前台 + 尝试抓取（不抢前台） |
| `diag_douyin_fetch.py` | 单次抓取路径诊断（根/RenderWidget/节点数） |
| `diag_douyin_wait.py` | 等待抖音前台后，对比主线程与子线程抓取 |
| `diag_douyin_loop.py` | 前台循环重试，观察 accessibility 树是否构建 |
| `diag_douyin_deep.py` | 深度展开 UIA 树（定位空骨架） |
| `diag_douyin_attrs.py` | 探测隐藏属性（AutomationId / HelpText / Value） |
| `enable_douyin_a11y.py` | 点击「开启读屏标签」并验证内容树 |

## 托盘

| 脚本 | 用途 |
|---|---|
| `tray_native_test.py` | 无界面托盘功能测试（创建窗口/图标/消息循环/退出） |

## 待办

| 脚本 | 用途 |
|---|---|
| `test_todo_api.py` | 待办 REST API 集成测试（临时库 + 端口 8767，不碰真实数据） |
| `test_todo_window.py` | 窗口冒烟：临时库 + 端口 8768，显示 3 秒并截图 `_todo_window.png` |
| `test_todo_inline.py` | **单视图交互回归**（12 项）：＋ 号行内新建、点标题行内编辑、编辑期间轮询不打断、优先级三态循环、点 × 直接删除、内容不足/溢出时的滚动开关、**点任意空白自动保存**（编辑与新建）、编辑中点筛选先保存再切换、**真实点击 ＋ 号/标题不被全局回调吃掉** |
| `test_balloon.py` | 托盘气泡冒烟：真实弹一条，5 秒后清理临时图标 |

> 单元测试在 `../tests/test_todo.py`（CRUD / 排序 / 去重 / 免打扰 / 稍后提醒 / 启动补扫）。

## 仪表盘（编辑 / 交互）

| 脚本 | 用途 |
|---|---|
| `export_dashboard.py` | 导出静态快照（`仪表盘demo.html`） |
| `test_edit_api.py` | 编辑 API 集成测试（临时库，不碰真实数据） |
| `ui_test_timeline.py` | 用 CDP 驱动无头 Chrome 验证**缩放 / 悬停 / 还原按钮 / 打开编辑浮层** |
| `ui_edit_e2e.py` | 端到端验证 **改分类 → 撤销 → 删除段 → 抽屉撤销**（临时库 + 独立端口 8766） |

> 说明：两个 UI 测试需要 `websocket-client`，并会启动无头 Chrome。
> 关键 flag：`--disable-extensions`——本机被策略安装的扩展会拦截 localhost，
> 表现为页面永远 loading、`document.readyState=loading`。

## 已迁移

- `test_douyin_content.py`：内容级单测已迁移到 `../tests/test_core.py`，此处保留作历史参考。
