# 查询与大文件浏览架构

本文描述当前实现。重构范围与逐项验证记录见 [重构计划](maintainability-refactor.md)。

## 模块边界

| 模块 | 职责 | 运行线程 |
| --- | --- | --- |
| `cli.py` | 参数、日志初始化、应用退出码 | 主线程 |
| `tui/app.py` | UI 状态、请求编号、后台任务、结果切换与清理 | UI 线程；标记的 worker 方法在后台 |
| `query/execution.py` | 预览、等待继续、完整结果落盘、进度回调 | 查询 worker |
| `query/engine.py` | DuckDB 连接、SQL 验证、Arrow reader、预览预算 | 查询 worker；取消信号可跨线程 |
| `data/parquet.py` | footer、row group 索引、原始文件窗口 | 加载或页面 worker |
| `data/result_store.py` | 追加 IPC 批次、按行号回读、临时文件清理 | worker；磁盘操作由锁串行化 |
| `data/batch.py` | 行数/字节预算、窗口裁剪、Arrow 缓冲区压缩 | 调用方 worker |
| `data/view.py` | 页面协议、行数元数据、无 I/O 的 Arrow 缓存 | `TableData` 仅由 UI 线程操作 |
| `tui/widgets/arrow_table.py` | 视口、光标、列宽、格式化和绘制缓存 | UI 线程 |
| `tui/cell_formatter.py` | Arrow scalar 到有限长度的 Rich Text | UI 线程 |

渲染、光标导航、列宽测量都只访问内存。SQL 语义留在查询层，文件格式细节留在数据源。

## 原始文件浏览

1. 表格和编辑器先挂载。加载 worker 创建 `ParquetSource`，仅读取 footer，得到 schema、总行数和 row group 起始位置。
2. UI 建立 `TableData` 并显示表格。`peek()` 只查缓存：Python `None` 表示页面缺失，Arrow null scalar 表示实际空值。
3. `ArrowTable` 将一次消息循环里的缺失行合并为 `WindowRequested`，同时请求后续邻近行；占位单元格不进入格式化缓存。
4. 页面 worker 定位 row group 并解码批次，`PageBuilder` 收集请求窗口的有限前缀。完成后 UI 接收 `DataPage`，清除占位绘制缓存并刷新。
5. 页面不足以覆盖整个视口时，后续绘制继续请求缺失部分。滚动不要求完整文件常驻内存。

文件窗口保留 PyArrow 原始类型和行序。窗口读取发现文件大小或修改时间改变时，数据源要求重新 Browse。大 row group 内的定位仍可能需要解码前面的批次。

## SQL 生命周期

每次运行创建一个 `QueryControl` 和一个查询 worker。`QuerySession` 建立私有 DuckDB 连接，将文件注册为 `data` 视图，验证单条 SELECT（支持 WITH），随后消费 Arrow reader。

预览预算只限制输出，聚合和排序仍可读取完整输入。`QueryPreview.reason` 使用 `PreviewLimit` 表示暂停原因，`truncated` 由原因派生；展示文字由 UI 决定。

UI 用一个 `QueryPhase` 表示当前查询阶段：

| 阶段 | 含义 | 可用动作 |
| --- | --- | --- |
| `IDLE` | 无可继续的查询；可能显示原文件、完成结果或保留的旧结果 | Run、Browse |
| `RUNNING` | 执行 SQL 并读取预览 | Cancel、Run、Browse |
| `PREVIEW` | 预览截断，worker 等待用户决定 | Load all、Cancel、Run、Browse |
| `MATERIALIZING` | 将剩余结果写入临时结果存储 | Cancel、Run、Browse |

查询完成、失败或取消后进入 `IDLE`。模态界面打开时，上述查询快捷键暂不可用。`query_running` 和 `can_load_all` 只由阶段派生，不单独写入。

阶段描述当前请求，显示的数据可以来自之前的请求。运行新查询不会立即抹掉旧结果；失败或取消也保留已经显示的内容。`_has_result` 包括成功的空结果，用于决定是否需要初始 loading 遮罩。

截断预览保留 DuckDB reader 和尚未消费的批次尾部。Load all 唤醒同一个 worker，先将预览写入 `ResultStore`，再追加 reader 的剩余输出。不会重跑 SQL，也不会通过 LIMIT/OFFSET 重建页面，所以随机值和输出顺序保持一致。结果中的行号是输出序号。

## 线程、所有权与取消

`execute_query()` 不依赖 Textual。它在 worker 上同步调用 `on_preview`、`accept_store`、`on_progress`、`on_error`；`ParqxApp` 通过 `_call_on_ui_thread()` 将这些操作同步转交 UI。

`accept_store()` 的返回值是资源所有权交接点，不能改成不等待结果的消息发送：

- 返回 True 前，执行函数负责清理新建的 `ResultStore`，包括写入失败、取消和 UI 拒绝过期请求。
- 返回 True 后，UI 已登记存储并将它作为当前读取源。执行函数继续追加，但不再负责删除；错误或取消后，已显示的前缀仍可回读。
- 切换显示源时，UI 调度清理 worker。退出时还会清理所有已接受但尚未关闭的存储，覆盖清理任务尚未启动就被 Textual 取消的情况。
- `ResultStore.close()` 与读取、追加共用锁，可重复调用。DuckDB reader、连接和 spill 目录始终在查询 worker 上释放。

`QueryControl` 的事件负责跨线程通信：`started` / `finished` 表示 worker 生命周期，`load_all` 唤醒暂停的预览，`cancelled` 配合连接的 interrupt 停止计算。取消同时设置 `load_all`，确保暂停中的 worker 能退出。这些事件不替代 UI 阶段。

新查询或 Browse 会取消旧 control 并递增请求编号；所有 UI 回调都验证编号。页面读取另外使用页面编号、取消事件和 `TableData` 身份，避免迟到页面污染新结果。退出时先发取消信号，再在后台清理存储、等待已启动的查询释放资源；等待上限由 `QUERY_SHUTDOWN_TIMEOUT_SECONDS` 定义。

## 预算与缓存

各预算作用不同，调整时不要因为默认数字相近而把它们合并。

| 位置 | 默认限制 | 用途 |
| --- | --- | --- |
| `QueryLimits` | 10,000 行 / 32 MiB 预览；1,024 行批次 | 输出预览与消费粒度 |
| DuckDB 配置 | 256 MB memory limit；2 个线程 | 查询执行与 spill |
| 数据源页面 | 256 行 / 4 MiB | 一次后台窗口的返回值 |
| `TableData` | 32 MiB / 128 页 | 已读取 Arrow 页面的 LRU |
| `TableData` 宽度样本 | 256 行 / 256 KiB | 从首个可用页面保留独立的小样本 |
| `ArrowTable` 宽度测量 | 最多 256 个样本行 | 估计格式化后的终端宽度 |
| `ArrowTable` 预取 | 缺失范围后 256 行 | 合并读取需求，由数据源进一步裁剪 |
| 格式化 / 单元格渲染缓存 | 各 10,000 项 | 已访问单元格的 Text / segments |
| 行 / 屏幕行缓存 | 各 1,000 项 | 固定与滚动 segments / 裁剪后的 Strip |

页面和预览均允许首行超过字节预算，保证超大值也能被检查。Arrow slice 共享底层缓冲区，应避免小窗口长期引用整个大批次；`PageBuilder` 和宽度样本用 `take()` 压缩保留部分。字节预算按 Arrow 的实际 `nbytes` 计算，包括可能存在的有效性位图。

这些预算不构成进程内存硬上限：解码批次、DuckDB 执行和渲染对象各自占用内存。进度回调按 `PROGRESS_INTERVAL_SECONDS` 节流，读取到 EOF 时发布最终行数。`row_count` 表示已可浏览的行数，`total_rows=None` 表示尚未到 EOF，不另发 COUNT 查询。

表格缓存分为三个失效范围：

- 格式化值来自不可变 scalar；padding、光标、斑马纹等变化保留它们。数据替换或主题更新会清除。
- 单元格 segments、整行和屏幕 Strip 依赖样式与布局，统一通过 `_clear_render_caches()` 失效。
- 列宽和列偏移只在宽度样本、数据源或 padding 改变时重算。流式行数增长只更新索引列；昂贵的虚拟尺寸计算合并到 idle。

每个单元格只占一行；`RenderedRow` 明确分开固定索引与可滚动内容。水平裁剪相对于第一个已渲染列的位置计算，鼠标元数据始终使用数据行/列坐标。表格消息仍定义在 `ArrowTable` 内，保留 Textual 的消息命名。

## 修改与验证

增加数据源时实现 `WindowSource.read_window()`，在 worker 内读取，并向 `PageBuilder` 提供有绝对偏移、按顺序连续的批次。取消使用共享的 `ReadCancelledError`；不要让渲染器直接调用数据源。

改变查询流程时，先明确 phase 转移、请求编号检查和存储所有权。新增回调必须保持同步交接语义；错误处理不得顺手删除已经交给 UI 的结果。

常规检查：

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright --pythonpath .venv/bin/python
.venv/bin/mypy src
.venv/bin/python -m pytest -q
```

`tests/unit/` 覆盖窗口预算、Arrow 类型保真、查询预览/续读、存储所有权和渲染边界；TUI 集成测试覆盖导航、键盘、模态界面、取消、过期请求和关闭清理。共享的 `tests/helpers.py` 提供消息循环等待、命令面板导航和 `WorkerGate`；并发场景用事件建立边界，避免用 sleep 猜测执行时机。

`tests/smoke_test.py` 用于已安装 wheel/sdist 的发行验证，保持只依赖标准库与运行时依赖。增加模块时同步更新模块清单。PyArrow 本地类型声明的维护方法见 [typings 说明](../typings/README.md)。
