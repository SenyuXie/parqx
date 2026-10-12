# Parqx 原生组件重构计划

状态：待实施。目标分支：`refactor`。

本文汇总本次架构调整的设计决定，包括统一配置、移除主动中断、分页防抖，以及直接使用 Arrow 数据。文中接口表示目标设计，不代表当前代码已经完成迁移。

## 1. 目标与设计边界

以 Textual 管理界面和交互、DuckDB 执行查询、PyArrow 承载数据。Parqx 只实现文件身份、预览预算、分页缓存等应用需要的能力。

- 直接组合原生组件和调用原生 API，不增加 `TableWorkspace`、`ConfigManager`、查询 runner、通用任务管理器或事件总线。
- 公共能力通过现有组件、明确的数据记录和普通函数复用；不为减少单个文件的长度而新增转发层。
- 延续已经完成的 pane 合并，以及将 batch 辅助函数合入 `data.duckdb` 的调整。
- 数据层不依赖 Textual；UI 通过后台任务调用同步的数据操作。
- 本次调整内部 API，同步迁移所有项目内调用方，不保留旧包装的兼容别名，不升级依赖。

产品假设是查询通常较快，允许已开始的原生操作自然完成。结果只显示前若干行并不保证执行时间：排序、聚合等阻塞算子仍可能先处理全部输入。因此，移除主动中断是明确的交互取舍，预览预算仍需保留。[DuckDB 阻塞算子说明](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads#blocking-operators)

## 2. 统一配置：`parqx.config`

新增 `parqx.config.ParqxConfig`，使用不可变、关键字初始化的 dataclass，替代现有 `QueryLimits`。模块只负责配置字段、默认值和校验，不持有连接、worker、缓存或界面状态。

### 配置字段

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `memory_limit` | `"256MB"` | 每个 DuckDB 连接的执行内存设置 |
| `threads` | `2` | 每个 DuckDB 连接的执行线程设置 |
| `preview_rows` | `10_000` | 每份 SQL 预览最多保留的行数 |
| `preview_bytes` | `32 * 1024 * 1024` | 每份 SQL 预览的 Arrow 逻辑字节预算 |
| `query_batch_rows` | `1024` | SQL Arrow reader 的 batch 行数 |
| `page_rows` | `4096` | 每次来源读取的最大行数及预读目标 |
| `page_bytes` | `4 * 1024 * 1024` | 每次来源读取的 Arrow 逻辑字节预算 |
| `page_cache_bytes` | `32 * 1024 * 1024` | 每个来源 pane 的分页缓存预算 |
| `source_workers` | `4` | 来源元数据和分页读取线程池的并发数 |
| `page_debounce_seconds` | `0.1` | 最后一次有效缺页请求到允许提交读取的间隔 |

这些预算相互独立。DuckDB 内存设置、保留的 Arrow 数据和 UI 渲染缓存不能相加视为严格的进程内存上限；SQL 预览和来源页面继续允许一行超过字节预算，以保证数据可查看。

校验规则：行数、字节数和线程数必须为正整数；防抖时间必须是有限的非负数，`0` 表示不延迟提交，但仍限制每个 pane 只有一个已提交任务；内存设置不能为空。DuckDB 内存单位和格式由 DuckDB 自身验证，不实现另一套解析器。创建或导入配置模块不执行 I/O。

### 配置传递

```python
config = ParqxConfig(page_debounce_seconds=0.1)
app = ParqxApp(paths, config=config)
```

- `ParqxApp(paths, *, config: ParqxConfig | None = None)` 在未传入配置时创建默认配置，并持有本次运行的配置实例。现有 CLI 使用默认配置，不新增命令行选项。
- `QueryScreen`、`TablePane` 和应用查询入口使用这份配置，不维护自己的另一份预算定义。`query_limits` 参数及 `QueryLimits` 类型被移除。
- `execute_query(sources, sql, *, config: ParqxConfig, unavailable=())` 接收应用配置，并把查询所需字段传给底层操作。
- `connect(*, memory_limit, threads)`、`read_preview(reader, *, row_limit, byte_limit)`、`PageCache(..., cache_bytes=...)` 等基础接口只接收所需参数。
- 构造 `ParquetSource` 时显式传入分页预算和 DuckDB 执行设置；来源元数据读取和后续窗口读取使用相同设置。
- 默认配置值只在 `ParqxConfig` 中定义；迁移后的内部调用显式传参，避免在不同模块重复维护这些默认值。

以下内容继续由所属模块管理：Textual CSS、widget 显示属性、列宽采样数量、来源 reader 的内部 batch 大小、缓存的页数保护上限，以及日志格式和轮转策略。当前 worker、活动标签页、缓存内容、滚动位置都是运行状态，不纳入配置。

本轮不增加 TOML 文件、环境变量覆盖、热更新、可变全局配置单例或配置管理框架。

## 3. Textual 组件与 SQL 生命周期

### 组件职责

| 组件 | 职责 |
| --- | --- |
| `App` | 直接组合 `TabbedContent`，持有配置、来源目录和来源线程池，协调标签页及查询结果 |
| `TablePane(TabPane)` | 统一显示来源与结果，处理来源加载、错误、分页调度和缓存所有权 |
| `ArrowTable(ScrollView)` | 直接渲染 Arrow 数据，处理虚拟滚动、导航和缺页消息 |
| `QueryScreen(ModalScreen)` | 编辑 SQL，启动查询，展示运行状态及错误 |

父组件通过属性和方法更新子组件，子组件通过 Textual `Message` 向上报告；使用 `compose()`、`DEFAULT_CSS`、`@on`、actions 和 reactive/watch 管理各自职责。保留 `ArrowTable` 的虚拟渲染能力和现有 `CellFormatter`，不另建布局辅助层。[Textual 数据流约定](https://textual.textualize.io/guide/widgets/#data-flow)

### SQL 执行

- `QueryScreen` 使用 `@work(thread=True, exit_on_error=False)` 执行同步查询，通过原生 `Worker.StateChanged` 在 UI 线程处理结果和错误。
- 只保留当前 worker 引用；从提交到完成事件处理结束，编辑器只读，关闭、换行和再次执行均禁用。`action_close()` 本身也检查运行状态，不能仅禁用快捷键。
- 成功后关闭弹窗并显示结果；失败后展示错误、恢复编辑和关闭能力，并将焦点还给编辑器。保留编辑历史与提交时的来源快照。
- 工作线程不直接更新界面。处理完成事件时核对 worker 身份和界面生命周期，避免在退出或界面失效后回写。
- 删除 `QueryControl`、`QueryCancelledError`、中断重试线程、相关锁、取消检查、原生任务开始/完成事件，以及退出时的轮询等待。
- 删除 `QuerySession`，将一次性执行流程直接放入 `execute_query()`。来源注册需要独立函数时使用模块私有函数，不新增类。
- connection、relation 和 reader 都由执行线程创建和使用，通过上下文管理依次释放 reader、connection 和临时目录。某个清理步骤出错时，其余资源仍需释放。
- 保留单条 SELECT/WITH 校验、来源不可用警告、预览预算、执行耗时和查询错误包装。

应用退出仍由 Textual 和 executor 完成任务收尾：停止界面更新和新任务调度，取消尚未开始且可取消的排队任务；已经进入 DuckDB 的操作自然完成并释放资源。不再提供主动中断或固定时间内结束进程的保证，也不把 worker 的取消状态当作原生线程已经结束。[Textual worker 说明](https://textual.textualize.io/guide/workers/#thread-workers)

## 4. 来源分页：防抖与有限任务数量

保留 `ArrowTable` 对同一帧内缺页请求的合并。在 `TablePane` 中直接使用 Textual timer 和 worker 完成调度，不新增防抖工具类或任务队列类。

1. 收到有效的 `WindowRequested` 后保存最新窗口，更新请求序号，并重启单次 timer。默认等待 `page_debounce_seconds = 0.1` 秒；缓存命中仍立即显示。
2. 每个 pane 最多一个已提交的读取任务，包含在线程池中等待的任务。其余请求只保留最新一个窗口，不继续向 executor 提交。
3. 读取期间发生新请求时覆盖待处理窗口。当前读取结束且最新请求的防抖时间已到后，提交最新窗口；若防抖尚未到期，则继续等待 timer。
4. 结果返回时核对来源、缓存身份和请求序号。过期结果与错误直接丢弃，不清除新请求的状态，不触发旧请求的错误提示。
5. 当前有效读取成功后更新缓存并刷新表格。受字节预算影响只返回部分页面时，允许表格继续请求剩余缺页；有效读取失败后保留现有导航触发重试行为，避免重绘自动重试循环。
6. 关闭 pane 时停止 timer、清除待处理窗口并释放缓存引用。已开始的读取自行结束，不更新已关闭界面。替换来源或缓存同样使旧请求失效。

只有真实读取返回后才释放该 pane 的读取名额。不能通过取消旧 Textual worker 来启动新的原生读取，否则仍可能积压后台操作。

- 分页 worker 移除通过 `exclusive=True` 反复取消旧任务的模式；worker 继续归属于 pane。
- 来源元数据和分页读取继续使用 App 持有的来源线程池，SQL 使用 Textual 线程 worker 的执行通路，避免来源池繁忙阻塞 SQL 调度。
- `ParquetSource.read_window(start, stop)` 移除控制器参数；保留文件变更检测、范围裁剪、预读和字节预算。
- 读取任务仅接收来源及范围，不持有表格和缓存。待处理请求和结果匹配需要缓存身份时使用弱引用，确保关闭 pane 后缓存可释放。

## 5. Arrow 数据通路与保留的公共能力

### 直接使用原生数据

- 删除 `TableData.from_table()` 及持有整张已加载表的分支。已加载查询结果直接使用 `pa.Table`。
- 将现有 `TableData` 收窄并更名为 `PageCache`，保留在现有数据模块中，仅保存来源 schema、总行数和有界分页缓存。
- `ArrowTable` 接受 `pa.Table | PageCache`：前者直接访问 Arrow scalar，后者查缓存并在缺页时发送消息。
- 统一通过 `replace_data(pa.Table | PageCache)` 替换数据，删除 `replace_table()` 转发入口。替换时保持现有导航、布局和渲染缓存重置行为。
- `TablePane` 的数据输入收窄为 `pa.Table | SourceSpec`，来源缓存由 pane 根据配置创建。
- 保留 `DataPage(start, table)` 表达绝对行偏移。分页缓存继续区分未加载和 Arrow null，保留淘汰规则、页数保护上限及单页超限例外。

### 展平查询结果

- 删除 `QueryPreview`，`read_preview()` 返回 `tuple[pa.Table, str | None]`，分别表示保留数据和截断原因。
- `QueryResult` 直接保存 `sql`、`table`、`elapsed`、`issues` 和 `truncation_reason`；UI 使用 `result.table`。
- 调用方直接从类型的定义模块导入，不增加兼容转发层。

### 保留有实际职责的代码

| 能力 | 保留理由 |
| --- | --- |
| `SourceCatalog` 及来源记录 | 文件去重、稳定的 SQL 表名和来源状态 |
| `ParquetSource` | 元数据快照、文件变更检测和按窗口读取 |
| `PageCache`、`DataPage` | UI 同步渲染所需的有界分页缓存及偏移信息 |
| `QueryResult`、`QueryExecutionError` | 查询结果与耗时、来源警告、显示错误信息 |
| `data.duckdb` 中的共享函数 | 连接设置、预览预算及 Arrow buffer 压缩 |
| `CellFormatter` | Arrow scalar 到单元格显示内容的转换 |

此前合入 `data.duckdb` 的 batch 函数继续留在该模块，保持字典和嵌套类型、时间精度及截断后的 buffer 释放行为。Arrow 的零拷贝切片不能直接替代现有压缩逻辑，否则可能继续保留被截掉的数据缓冲区。

## 6. 实施顺序与验收

### 实施顺序

1. 新增 `ParqxConfig` 并迁移配置来源，删除 `QueryLimits`，让 App、SQL 和来源读取使用统一配置；此阶段先保持现有执行行为。
2. 一并移除 SQL 和分页的主动中断，简化查询执行与弹窗状态，并接入分页防抖和有限任务调度，避免出现无中断却无限提交读取的中间状态。
3. 简化 Arrow 数据通路、展平查询结果，迁移所有内部调用、类型导入、测试和分发检查。
4. 更新 README 的 Esc 行为说明及相关架构说明，完成全量验证。保留 Textual 衍生代码的署名和 NOTICE。

### 必须覆盖的行为

- 配置：默认值保持现有预算；覆盖值传到 SQL 连接、来源连接、预览、缓存、线程池和防抖 timer；非法数值被拒绝；导入和构造配置不产生 I/O。
- SQL：正常查询、无效 SQL、部分来源不可用、空结果、行数/字节截断及原因展示正确；运行期间 Esc、重复 Enter 和直接调用关闭动作均不会关闭或重复执行；成功或失败后状态恢复。
- 防抖：连续请求在防抖到期后只提交最新窗口；同一 pane 已提交任务数始终不超过一；读取期间只保留一个待处理窗口。
- 调度：防抖到期早于或晚于当前读取结束，都能最终提交最新窗口；`page_debounce_seconds = 0` 仍维持任务数量限制。
- 生命周期：关闭 pane、替换数据、应用退出后，旧结果和错误不再回写；弱引用对应的缓存能够释放；来源线程池繁忙时 SQL 仍可执行。
- 数据：部分页可以继续填满视口；缓存淘汰和导航后重试不产生重绘读取循环；null、字典、嵌套类型和时间精度保持正确。
- 资源：查询成功、查询异常、reader 清理异常和界面退出路径最终释放 reader、connection 及临时目录。

将原有“必须主动中断”以及固定退出等待预算的测试替换为新交互和生命周期测试。依赖 `QuerySession` 内部方法的测试迁移到执行入口和资源边界，保留有意义的数据正确性及清理断言。

防抖和并发测试使用可控的完成信号与 timer 回调验证提交顺序，避免依赖长时间真实休眠。无需为纯转发或命名变化增加测试。

完成完整 pytest、Ruff lint/format 检查、mypy、使用项目解释器的 Pyright，以及现有 wheel/sdist 分发安装检查。本次不新增功能、配置文件机制、性能基准或截图测试项目。
