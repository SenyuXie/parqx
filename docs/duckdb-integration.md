# DuckDB 查询与大文件浏览计划

本文记录 Parqx 引入 DuckDB 的完整方案、实现顺序和验收标准。DuckDB 负责 SQL 执行与数据裁剪，Arrow 作为数据交换格式，现有 ArrowTable 继续负责终端交互和绘制。

## 当前架构与问题

当前链路为 `Typer → ParqxApp → 后台 pq.read_table → 完整 pa.Table → ArrowTable → CellFormatter → Rich/Textual`。

- `app.py` 在后台完整读取文件后才挂载表格；首屏等待完整读取，内存与解压后数据量相关。
- `ArrowTable` 同时负责数据访问、列宽、导航、缓存和绘制，假设数据完整且不可变。
- 表格已经具备视口裁剪、局部刷新和多层 LRU 缓存，应保留这些实现。
- 部分类型的列宽使用整列 `min_max`，布局可能扫描全部数据。
- 首次格式化一行时处理全部列，即使视口只显示其中几列。
- 样式缓存清理有意保留格式化缓存；查询替换结果时必须另行失效。

分析时的基线为 22 项测试通过。500 列的合成表在 100 字符宽的区域显示约 8 列，但首次行格式化调用了 500 次 formatter。仓库示例文件不足以验证大数据性能。

## 目标架构

```text
CLI / ParqxApp
    └── 查询与加载控制器（后台执行、取消、请求版本、状态）
        ├── 原始浏览：PyArrow ParquetFile / row groups / RecordBatch
        └── SQL 查询：DuckDB 文件视图 data
            ↓
        数据访问层（Schema、行数状态、Arrow 批次、有限缓存）
            ↓
        ArrowTable（视口与光标、缓存内访问、可见单元格格式化）
            ↓
        CellFormatter / Rich / Textual
```

### 核心决策

1. SQL 直接查询 Parquet 文件，以 `data` 作为当前文件的视图名，不依赖先全量加载源文件，不预先导入持久化数据库。
2. 保留 PyArrow 原始浏览路径与原始 Schema。查询类型和原始类型可能不同，例如 DuckDB 对部分带时区纳秒时间戳的精度转换。
3. 查询输出使用 Arrow，避免 Pandas、`fetchall()` 和逐行 Python 字典转换。
4. 渲染和光标路径只读取缓存。缓存缺失显示占位，后台完成后按区域刷新，不同步执行 SQL 或文件 I/O。
5. 每次数据替换具有新的版本，失效所有数据相关缓存；旧请求的结果不能覆盖新请求。
6. 查询结果按一次执行的输出顺序保存。滚动不重新执行 `LIMIT/OFFSET`；结果中的显示行号不是原文件行号。
7. 原始文件总行数来自 footer；流式结果区分已加载行数和未知总行数。总数未知时不额外执行 `COUNT(*)`。
8. SQL 首期支持产生结果集的单条查询。预览上限作用于最终结果，不能先截断参与聚合的输入。

## 阶段一：SQL 预览与渲染基础

- [x] 单元格惰性格式化；不可见列不参与格式化。
- [x] 列宽使用有限样本，避免整列扫描；保留 Unicode、null 和嵌套值预览行为。
- [x] 表格提供统一的数据替换入口，处理 Schema、列宽、缓存、光标与滚动状态。
- [x] 封装 DuckDB 会话、文件视图和 Arrow 输出；添加依赖及锁文件。
- [x] SQL 编辑、执行、取消、错误展示、返回原始浏览；提供 CLI 初始查询入口。
- [x] 默认最多预览 10,000 行，并设置批次与缓存字节预算，明确显示截断状态。
- [x] 查询错误保留之前的结果；查询结束、取消和退出时释放资源。

阶段验收：筛选、聚合、排序、CTE、空结果、含特殊字符的文件路径能够正常工作；结果替换后无旧值或旧样式残留；宽表的格式化开销与访问单元格数量相关。

## 阶段二：按需加载与完整结果浏览

- [x] 提取小型数据访问接口，包含 Schema、已加载行数、总行数状态、缓存访问和数据窗口请求。
- [x] 原始浏览读取 footer 后即可显示，按 row group 定位、按批次读取并预取邻近窗口；保持原始行序和类型。
- [x] 数据缓存按字节预算淘汰，限制预取；大 row group 不按整组强制常驻内存。
- [x] SQL 一次执行、按 Arrow 批次接收；预览之后提供明确的完整结果加载操作。
- [x] 完整结果可按批次落盘并建立位置索引，以有限内存支持往回滚动；关闭或替换时清理临时文件。
- [x] 缓存缺失使用独立的 loading 占位，不混淆 SQL NULL；加载完成后刷新可见区域。
- [x] 已加载行数与总行数分别展示，完整结果末尾在读取完成后可定位。
- [x] 新查询或视图切换淘汰过时请求，不在每次滚动时重跑 SQL。

阶段验收：默认打开不调用全量 `pq.read_table`；跨 row group 导航、首末行跳转、水平滚动、回滚浏览正确；大结果的常驻缓存有界；未知行数不触发额外计数查询。

## 阶段三：执行控制与集成验收

- [x] 查询连接由后台执行流程管理；不在 UI 线程消费 Arrow reader。
- [x] 真正取消 DuckDB 执行，结合请求编号避免迟到结果覆盖；取消后可继续查询。
- [x] 分别设置 DuckDB 内存/线程/临时目录和应用 Arrow 缓存预算。
- [x] 明确 DuckDB `memory_limit` 不等于进程硬上限，单个超大值和解码批次可能超出缓存预算。
- [x] 排序、聚合等阻塞算子执行期间保持界面响应并允许取消。
- [x] 更新 README、使用说明、测试和打包 smoke checks。
- [x] 完成 pytest、Ruff、mypy、Pyright；测试资源清理、错误恢复、连续提交和类型保真。

## 实现约束

- 先让有限结果预览可用，再替换底层全量数据依赖，保持每一步可验证。
- 现有 ArrowTable 导航和 CellFormatter 保留；不要把 SQL 语义放入渲染器。
- 行数预算不能代替字节预算；LRU 淘汰后也要避免格式化对象长期引用原始批次。
- Parquet 不是任意单元格的常数时间存储；大 row group 内定位可能仍需解码相关数据。
- Arrow reader 分批输出不保证所有查询立即输出首批；排序、聚合、窗口函数等可能先扫描输入。
- 单条用户查询、缓存/临时文件、DuckDB reader 与连接都具有明确的生命周期。

## 实施记录

- 2026-09-26：完成架构分析，建立本计划；开始阶段一。

- 2026-09-26：完成单元格惰性格式化、有限采样列宽和表格替换入口；新增宽表调用次数、整列扫描防回归与缓存替换测试。

- 2026-09-26：完成 SQL 预览交互与 CLI 查询入口，查询生命周期及错误恢复通过 TUI 交互测试。

- 2026-09-27：默认浏览已改为 footer + 后台窗口读取。十万行首末导航、读取阻塞时的 UI 响应、迟到窗口不能覆盖 SQL 的交互测试通过。

- 2026-09-27：完整 SQL 结果可继续同一次执行并落盘回读；两万行随机值测试验证预览值保持、只执行一次、末尾/开头导航及退出清理。

- 2026-09-27：完成 58 项测试、Ruff、mypy、Pyright、发行包构建及安装后的 smoke check。

## 交付与审查

实现分支为 `codex/duckdb-query`，从 `c89f3c5` 创建。提交按计划文档、渲染基础、查询引擎、SQL 交互、数据窗口、按需浏览、结果存储、完整结果加载、状态边界修复、关闭清理和集成验收分别拆分。分支留在本地，供后续推送并创建 PR。

实现位置：`query/engine.py` 管理 DuckDB 会话与 reader；`data/` 管理窗口、缓存和临时结果；`tui/app.py` 协调后台执行与请求版本；`ArrowTable` 只同步访问缓存。

## 参考资料

- [DuckDB Parquet 扫描与下推](https://duckdb.org/docs/current/data/parquet/overview)
- [DuckDB Python API、Arrow reader 与 interrupt](https://duckdb.org/docs/current/clients/python/reference/)
- [DuckDB 时间戳类型](https://duckdb.org/docs/current/sql/data_types/timestamp)
- [DuckDB 内存配置](https://duckdb.org/docs/current/configuration/pragmas#memory-limit)
- [DuckDB 阻塞算子与落盘](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads)
- [PyArrow ParquetFile 批次与 row group API](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetFile.html)
