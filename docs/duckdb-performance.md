# DuckDB 与按需浏览验收记录

## 复现方式

```bash
uv sync --locked --group dev
uv run python benchmarks/duckdb_browsing.py
uv run pytest -q
uv run ruff check
uv run ruff format --check
uv run mypy src
uv run pyright
```

性能脚本自动生成三组 Parquet 数据，并为每个场景启动独立进程。完整原始结果见 [duckdb-benchmark-results.json](duckdb-benchmark-results.json)。

本次运行日期：2026-09-27；环境：macOS 26.5.1 arm64、Python 3.12.14、DuckDB 1.5.5、PyArrow 24.0.0、Textual 8.2.7。运行在本机、无 CPU 探测沙盒限制的进程内。

测量范围：Textual 无头测试环境，视口为 120 × 40；首屏指标从创建后的 App 启动到首个窗口有值并完成刷新，包含事件循环调度，不包含 Python 导入耗时和实体终端绘制。数据刚生成，操作系统文件缓存通常为热缓存；“跳到末行”中的冷缓存仅指应用尚未加载目标数据。每个场景只运行一次，这些数值是工程检查记录，不是延迟 SLA、统计分位数或与旧版本的提速比较。

## 本次结果

| 浏览场景 | 首个有值视口 | 跳到末行 | 跳转新增 formatter 调用 | 峰值 RSS |
| --- | ---: | ---: | ---: | ---: |
| 1,000,000 行 × 3 列，row group 100,000 行 | 124.81 ms | 95.05 ms | 114 | 100.77 MiB |
| 2,000 行 × 500 列 | 206.95 ms | 116.41 ms | 444 | 231.50 MiB |
| 10,000 行，4 KiB 文本与嵌套结构 | 124.29 ms | 93.17 ms | 76 | 107.30 MiB |

宽表跳转时处理的是视口内单元格；没有按每行 500 列重新格式化。首屏另外包含有限列宽采样，不能直接用跳转调用数代表首屏工作量。

| SQL 场景（百万行输入） | 结果预览 | 峰值 RSS |
| --- | ---: | ---: |
| 按 id 筛选末尾 1,000 行 | 7.70 ms | 90.81 MiB |
| 按 100 个组聚合 | 9.23 ms | 90.41 MiB |
| 完整排序后的前 10,000 行 | 32.98 ms | 128.91 MiB |
| 查询前 100,000 行并将全部结果写入 Arrow 临时批次 | 预览 7.44 ms；完整写入 15.98 ms | 97.14 MiB |
| 中断十亿次三角函数计算的查询 | 取消及连接清理 0.92 ms | 88.12 MiB |

完整写入计时不包括关闭临时目录的删除时间，也不表示磁盘已完成持久化同步。这里的临时结果只用于会话内浏览。

## 自动化覆盖

| 要求 | 验证入口 |
| --- | --- |
| 可见单元格格式化、有限宽度采样、替换结果无旧缓存 | `tests/unit/tui/test_arrow_table.py` |
| 字符串、二进制、时间间隔和嵌套格式化 | `tests/unit/tui/test_cell_formatter.py` |
| 带引号路径、CTE、聚合、空结果、行/字节预览预算、继续读取 | `tests/unit/test_query_engine.py` |
| 仅 footer 打开、跨 row group、文件变化、缓存淘汰、原始纳秒值 | `tests/unit/test_data_windows.py` |
| Arrow 批次随机回读、字节预算与目录清理 | `tests/unit/test_result_store.py` |
| 十万行首末导航、慢 I/O 时继续操作、旧窗口不覆盖 SQL | `tests/test_lazy_browse.py` |
| 查询错误恢复、连续提交、中断执行、完整加载、中途取消、随机值保持、替换/退出清理 | `tests/test_query_app.py` |
| CLI 参数、入口和发行包模块 | `tests/test_cli.py`、`tests/smoke_test.py` |

最终本地检查：**58 项 pytest 测试通过**；Ruff 检查与格式检查、mypy、Pyright 均通过。源码包和 wheel 构建成功；将 wheel 安装到独立目录，确认导入路径来自该目录后，模块、类型标记、版本和 CLI 入口 smoke checks 通过。打包检查复用已安装的第三方运行依赖；CI 的隔离安装仍需 PR 实际运行确认。

## 资源边界与后续评估

- 原始文件和落盘结果共用缓存访问接口。Arrow 页面缓存默认 32 MiB，独立的紧凑列宽样本最多约 256 KiB；格式化缓存按单元格数限制。
- 预览默认 10,000 行、约 32 MiB；一个超大行允许突破预算以保证可查看。DuckDB 使用独立的 256 MB 内存设置和 2 个执行线程。
- 这些不是进程 RSS 硬上限。引擎执行、Parquet 解码、Python/Rich 对象、一个超大值及结果批次索引都另占内存，宽表结果说明了这种区别。
- 完整结果将数据量压力转移到临时磁盘，批次位置索引仍随批次数增长。磁盘不足时保留已经显示的结果并报告查询错误。
- 对一个特别大的 row group，定位到靠后位置仍可能需要解码前面的批次；没有宣称任意行随机读取为常数时间。
- 本地验收覆盖 macOS。仓库 CI 已配置 Linux/macOS/Windows 与 Python 3.12/3.13，跨平台结果需以后续 PR 的实际 CI 为准。
