# parqx SQL 编辑器界面调研

调研日期：2026-10-12。问题是：SQL 查询能否成为持续工作的主界面，而不是打开、提交、关闭的弹窗？比较 9 个图形产品和 4 个终端产品，并排除一个尚未提供 SQL editor 的候选，关注查询、结果、数据结构、历史和操作如何组织。

证据来自官方文档、官方发布文章和官方截图。**「结构」是可核实事实；「判断」是针对 parqx 的设计评价。**没有逐个安装实测；截图代表其所属文档/文章展示的界面，不等于每个平台、版本和配置的当前默认布局。界面美感主要按层级是否清晰、信息密度是否克制、操作和结果是否相邻来评价。

本文的调研建议保留当时的推理；后续已经确认的选择见末尾「设计讨论记录」，有差异时以确认记录为准。

## 图形界面样本

### 1. TablePlus：最值得借鉴的查询—结果关系

**结构：**SQL 编辑器作为独立 tab 存在，结果在下方。运行栏在编辑区底部、结果上方；Run Current/Run All、返回行数限制在同一区域。当前语句有高亮，多条语句的结果可以分 tab，执行记录进入 Query History。[编辑器文档](https://tableplus.com/docs/query-editor/editor-basics)

**判断：**值得学的是把「写 → 运行 → 看」放在连续的纵向路径上，操作栏自然成为分界。parqx 可采用这一骨架；多语句、连接管理、复杂运行选项不必一并引入。这里依据文档判断组织方式，官方图片地址返回 403，未将具体配色计入审美比较。

### 2. Beekeeper Studio：最适合作为简洁程度的标尺

**结构：**查询 tab 内上方编辑、下方结果；截图中 Save/Run 位于两区交界，底栏显示返回行数、受影响行数和耗时，下载入口在右下。多个结果通过状态栏下拉选择，历史从编辑器工具栏进入，并按连接隔离。[编辑器说明](https://docs.beekeeperstudio.io/user_guide/sql_editor/editor/) · [已目视核对的官方截图](https://docs.beekeeperstudio.io/assets/images/using-the-sql-editor-99.png)

**判断：**深灰底、少量语法色、很轻的边界，让代码和表格成为主角；Run 的对比度明显高于次要操作。parqx 可学「一个主动作＋紧凑状态行」。示例图给编辑区的高度很大，终端里需要更灵活的比例；暗色本身不是核心价值。

### 3. Postico 2：最清楚的资源导航与动作层级

**结构：**官方图左上是 Queries 文件夹/文件，左下是 Tables/Functions；右上 SQL、右下表格。两区之间依次安排 Query History、Cancel、显著的 Execute Statement；底边放结果切换、Export、行数和耗时。查询文件支持文件夹组织、自动保存；当前语句有浅灰背景。[产品页](https://eggerapps.at/postico2/) · [查询文档](https://eggerapps.at/postico2/documentation/sql-query-view.html) · [已核对截图](https://eggerapps.at/postico2/screenshots/query-light.png)

**判断：**它的「干净」来自职责分区和唯一强主按钮。查询和数据都保留在同一工作空间，用户不用丢掉上下文。适合借鉴动作位置；macOS 大按钮、宽松间距和两套常驻树会消耗终端空间，应压缩成可切换侧栏。

### 4. Supabase Studio：查询作为可持续管理的工作对象

**结构：**2025 年官方图是窄全局导航栏＋查询侧栏＋顶部查询 tabs＋中央代码区。侧栏有搜索、Templates/Quickstarts，以及 Shared/Favorites/Private 分组；tabs 支持 preview mode，避免浏览片段就留下大量标签。相关更新后来标注为 fully rolled out。[设计说明](https://supabase.com/blog/tabs-dashboard-updates) · [发布状态](https://supabase.com/changelog/35636-feature-preview-tabs-for-table-and-sql-editor) · [已核对截图](https://supabase.com/images/blog/lw14-tabs-dashboard-updates/sql-editor-tabs.jpg)

**判断：**低饱和底色、细线和清晰选中态，比繁复工具栏更安静。最值得学的是「查询有名字、有归属、可恢复」，而不是每执行一次多一个匿名结果。截图只展示编辑器上部，不能从这张图推断结果区。终端不宜同时常驻全局导航、查询列表和数据树。

### 5. DuckDB Local UI：最贴近 Parquet 数据探索

**结构：**左侧列 notebook、数据库与表；中央 notebook cell 把 SQL 与所属结果放在一起；右侧 Column Explorer 显示结果行列数、分布和列详情。左下另有选中源表摘要。SQL cell 可运行整体或选区，结果支持排序、筛选等操作。[官方介绍](https://duckdb.org/2025/03/12/duckdb-ui) · [已核对整体截图](https://raw.githubusercontent.com/duckdb/duckdb-web/main/images/blog/ui/basics.png) · [列摘要截图](https://raw.githubusercontent.com/duckdb/duckdb-web/main/images/blog/ui/column_explorer.png)

**判断：**突出优点是代码和结果有明确归属，同时区分「源数据」与「本次结果」；浅色背景、类型色和局部小图把概览塞进有限空间。parqx 可借鉴列摘要与查询归属；三栏＋多 cell 连续滚动对终端太重，完整 notebook 不必成为第一步。

### 6. MotherDuck：值得学结果探索，不必照搬整个 notebook

**结构：**官方研究文章将界面描述为 notebook，每个 cell 包含 SQL 编辑和交互结果；结果可做 pivot、筛选、聚合和排序。Column Explorer 提供紧凑分布、NULL 信息与统计，点击列再展开细节；官方细节图分别展示数值分位数、时间分布、类别频数。[官方研究](https://motherduck.com/research/motherduck-duckdb-in-the-cloud-and-in-the-client/) · [Column Explorer 设计说明](https://motherduck.com/blog/introducing-column-explorer/) · [已核对细节图](https://web-assets-prod.motherduck.com/assets/img/column-explorer-details.jpg)

**判断：**这是「先看概览，再按需展开」做得很好的参考。对 parqx 来说，可把类型、NULL 比例、distinct 数和范围放入可收起的结果详情。大量微型图、鼠标悬停与图形筛选不能原样搬进终端；资料描述的是相应发布时期，不用于宣称今天完整产品形态。

### 7. DataGrip：专业工作流参考，视觉上应做减法

**结构：**查询 console 绑定数据源；编辑器工具栏展示执行、事务、连接/会话及 schema。运行后 Services 工具窗口提供会话树、Output 和 Result tabs，结果还有独立工具栏。已核对文档图是上方代码、下方 Services，其中会话列表又占左侧。[Query consoles](https://www.jetbrains.com/help/datagrip/query-consoles.html) · [已核对截图](https://resources.jetbrains.com/help/img/idea/2026.2/db_ui_query_console_result_tab.png)

**判断：**优势是执行上下文显式可见，日志和数据明确分开。parqx 可以学「这份结果来自哪条查询、哪份数据」及就地输出；多套 tab、树和工具栏容易把轻量 viewer 变成 IDE，视觉密度不适合整体照搬。

### 8. DBeaver：面板控制与多结果管理的参考

**结构：**官方将 SQL Editor 明确拆成 Script panel、Toolbar、Result panel；编辑器内有结果 tabs，也可以在一个 tab 内展示/折叠多个结果。文档支持调整编辑区与结果区布局、显示/隐藏结果、放大工作区域；截图显示左侧竖工具栏、上方 SQL、下方表格和结果操作。[SQL Editor 文档](https://dbeaver.com/docs/dbeaver/SQL-Editor/) · [已核对截图](https://dbeaver.com/docs/dbeaver/images/ug/SQL-Editor.png)

**判断：**最有价值的是用户能够控制工作区域，而不是固定分屏比例。parqx 可以学面板放大和可恢复布局；小图标、竖向文字、结果两侧额外工具栏及多层边框会带来噪音，不宜作为视觉风格目标。

### 9. DbGate：统一数据表格的体验

**结构：**截图左侧是活动栏、连接区和表列表，右侧为查询 tab 与执行工具栏。文档明确：结果在编辑器下方，多结果分 tabs，Messages 单独承接消息、行数和错误；查询结果复用数据浏览器的表格引擎，因此筛选、排序、复制与导出一致。[SQL Editor 文档](https://docs.dbgate.io/dbgate/sql-and-queries/sql-editor/index.html) · [官方截图库](https://www.dbgate.io/screenshots/)

**判断：**很适合借鉴「浏览原表和查看 SQL 结果使用同一套表格交互」，减少认知切换。查询图核对的是编辑状态，结果布局由文档确认。终端应减少顶部常驻事务/参数/AI 按钮，并让错误持续留在输出区。

## 证据范围与优先参考

- **已视觉核对：**Beekeeper、Postico、Supabase、DuckDB UI、MotherDuck Column Explorer、DataGrip、DBeaver、DbGate 的官方静态图；未做实际交互测试。
- **仅核对文档：**TablePlus 的 tab、上下分屏、运行栏与历史。Supabase 图未包含结果区，DbGate 图未展示执行后状态，MotherDuck 本次只目视核对列详情。
- **优先看 4 个：**Beekeeper 的整体简洁度；Postico 的动作和导航层级；TablePlus 的编辑—执行—结果组织；DuckDB UI 的查询归属和列摘要。
- **共同设计启示：**持久编辑区、明确的代码/结果归属、临近的运行入口、可收起的辅助区，比把弹窗拉大更能解决持续查询的工作流问题。这是本次比较所得的设计推论。

## 终端界面样本

### 10. Harlequin：最直接的整体布局参考

**结构：**官方教程明确分为五个部分：左侧 Data Catalog；右上多 buffer Query Editor；右下 Results Viewer；两者之间的 Run Query Bar；底部随当前可用操作变化的 Footer。F10 可放大编辑区或结果区并恢复，Ctrl+B 可隐藏目录。官方也说明 Ctrl+Enter 可能被终端拦截，并提供替代执行入口。[官方布局教程](https://harlequin.sh/docs/getting-started/usage) · [功能概览](https://harlequin.sh/) · [官方界面图](https://raw.githubusercontent.com/tconbeer/harlequin/main/harlequin.svg)

**判断：**对 parqx 最有价值的是「常驻编辑＋常驻结果＋按需放大」；布局在终端行列限制下仍成立。目录可选，不必始终占据约四分之一宽度。本次核实了布局文档；网站截图受访问限制，SVG 未完成像素级检查，不将其配色作为审美结论。快捷键需要结合 parqx 的既有绑定和终端兼容性另行设计。

### 11. rainfrog：焦点与模式表达很克制

**结构：**官方演示可见左侧 schema/table 树，右上 Query / History / Favorites 共用区域，右下 Results。激活面板边框和标题变色，底部提示随焦点变化。文档提供各区直接跳转、调整面板大小和结果最大化。[官方项目与快捷键](https://github.com/achristmascarl/rainfrog) · [已抽帧核对的官方演示](https://raw.githubusercontent.com/achristmascarl/rainfrog/main/vhs/demo.gif)

**判断：**它的视觉层级来自薄边框、统一背景、有限高亮，而非很多盒子和按钮。历史、收藏复用编辑区上方的位置，比永远再添一个面板省空间。示例仍给短 SQL 留了很大编辑区，parqx 应允许缩小。可以学焦点表达，不必因此要求所有人接受 Vim 模式。

### 12. sqlit：适合借鉴底部上下文提示

**结构：**官方演示是左 Explorer、右上 Query、右下 Results；标题直接带 e/q/r 导航提示，底部两行分别承载连接/模式/执行反馈和当前快捷键。项目基于 Textual，文档包含历史、SQL 补全、结果筛选等能力。[官方 README](https://github.com/Maxteabag/sqlit) · [已抽帧核对的查询演示](https://raw.githubusercontent.com/Maxteabag/sqlit/main/docs/demos/demo-history.gif)

**判断：**同色系背景、稀疏分隔线和清楚的焦点边框，比装饰性终端框线更耐看。对 parqx 可借鉴「编辑时显示编辑操作，结果聚焦时显示表格操作」；不需要把 explorer、history 和收藏都常驻。这里的 Vim modal editing 指编辑模式，不是用户想移除的 modal screen，两者应区分。

### 13. lazysql：浏览数据优先的补充参考

**结构：**官方截图是左侧数据库树、右侧 Records / Columns / Constraints / Foreign keys / Indexes，下面直接接 WHERE 输入与大表格。项目 README 列出独立 SQL Editor 入口。[官方项目](https://github.com/jorgerojas26/lazysql) · [已目视核对的浏览界面](https://raw.githubusercontent.com/jorgerojas26/lazysql/main/images/lazysql.png)

**判断：**适合学「先看数据，轻筛选贴近表格」的任务组织，不能把这张 WHERE 截图当作完整 SQL editor 的证据。多层框线和每格边线相对更吵，可作为减法对照。

**排除说明：**gobang 的官方 README 仍把 SQL editor 列在 TODO 中，因此只可作为表格浏览参考，不列为成熟 SQL 编辑体验的推荐。[官方 README](https://github.com/TaKO8Ki/gobang)

## parqx 当前实现与问题

以下事实来自本地工作区，未修改产品代码：

- [QueryScreen](../../src/parqx/tui/screens/query.py) 是可重复打开的 ModalScreen，保留正文/选择/撤销历史；默认单行、Enter 执行、Shift+Enter 换行，宽度 80%、最多 90 字符，输入增高到最多 24 行。
- 执行时编辑器只读、不能关闭；成功后 dismiss，失败后显示 4 秒错误通知。成功不会清空编辑器，但会使 SQL 离开主视野。
- [ParqxApp](../../src/parqx/tui/app.py) 每次成功创建新的 Query N 结果 tab。SQL、耗时、完整/预览标识和不可用源警告主要放进 tab tooltip。
- [执行引擎](../../src/parqx/query/engine.py) 接受单条 SELECT（包括 WITH）；[默认配置](../../src/parqx/config.py) 预览上限为 10,000 行、32 MiB。这是文件查询/检查场景，没有必要照搬事务和数据库管理工具栏。

**设计诊断：**主要问题是「输入属于一个全局弹窗，输出属于一串独立 tabs」。一次持续的分析被拆成反复打开/关闭的动作；同一查询的草稿、最后一次执行的 SQL 和结果之间关系不够显式。修改配色或把弹窗放大并不能解决这个关系问题。

## 三种非弹窗组织方案

下面是根据样本归纳的 parqx 方案，属于设计建议，不是声称某一产品完全如此实现。

| 方案 | 布局及内容归属 | 最适合什么 | 代价 |
| --- | --- | --- | --- |
| **查询工作区（优先）** | 文件 tabs 继续展示原始数据；每个 SQL tab 内上方编辑、下方结果，侧栏按需展开 | 反复修改、执行、核对结果 | 需要把 SQL 草稿与对应结果放进同一查询对象 |
| **底部 SQL 栏** | 大表格优先；底部展开非遮挡编辑器，并明确指向对应 Query tab | 大部分时间浏览，只偶尔写短查询 | 全局草稿与多个历史结果容易混淆，必须显示关联关系 |
| **Notebook** | 每个 cell 同时拥有 SQL 与结果，多组沿页面排列 | 多步骤探索和记录分析过程 | 纵向滚动长、表格也需独立滚动，键盘焦点和空间成本最高 |

**优先选择查询工作区。**它最接近现有 tab 心智模型，也能让「修改 → 运行 → 查看」始终发生在原位。独立的全屏编辑页面也能去掉 modal，但仍需要在代码与结果之间来回切换，所以不作为默认方案；可将全屏作为同一工作区的临时放大状态。

### 第一版应明确的行为

1. **一次新查询创建一个 tab，同一查询重复执行更新本 tab 的结果。**如果要比较旧结果，再显式保留快照；不要每次运行都不断累积匿名 Query N。
2. **执行结束编辑器不消失。**默认上方 6–10 行左右，结果获取余下空间；支持收起与全屏放大。这个高度是原型起点，需要用真实 80×24、120×40 终端验证。
3. **侧栏按需打开。**多文件时列出实际可查询表名与列类型，选中项可插入 SQL。单文件且无需查字段时，把宽度让给数据。
4. **结果信息常驻一行。**显示已显示行数、完整/预览、耗时、来源警告。达到上限时写清「预览 10,000 行；总数未知」，不要暗示这是全部结果。
5. **区分草稿与已执行 SQL。**用户执行后再修改草稿时显示「结果来自上次执行」，避免把旧结果误认为新正文的输出；运行中保留旧结果并标记状态，失败后错误持续可见且可复制。
6. **让 Enter 回归多行编辑。**执行提供 F5 等清晰入口，可另支持 Ctrl+Enter，但不能把后者当作所有终端都可靠的唯一按键。焦点跳转、收起与关闭分开设计，不让 Esc 丢失草稿。
7. **保留统一表格体验。**原文件和查询结果继续共用现有 ArrowTable 导航。面板高亮仅表示焦点，语法色只表示语义；避免多层边框、常驻长按钮栏和过多 tab 层级。

### 优先级与后续边界

先解决常驻编辑、结果归属、状态展示、面板折叠/放大和焦点提示，再考虑查询命名、历史、字段补全。MotherDuck/DuckDB 风格的列统计可以以后按需加入；如果统计只覆盖预览数据，必须明确标注统计范围。

本次提供的是调研与布局草图，没有做实际终端可用性测试，也没有实现取消执行、结果快照或 notebook。后续设计验证应覆盖短查询、长 WITH、宽表、执行错误、空结果、截断预览、多个文件、切换旧查询这几种真实状态。

## 设计讨论记录

状态：Q1–Q15 的交互选择已由用户确认，测试边界亦已确认。正式规格已发布为 [GitHub Issue #28：persistent SQL query workspaces and source navigation](https://github.com/SenyuXie/parqx/issues/28)，标签为 `ready-for-agent`；产品功能尚未实现。

### 已确认

- **整体方向：**非弹窗查询工作区。支持多个独立工作区，各自保留 SQL 和当前结果，原始文件 tabs 继续保留。
- **结果归属：**重复执行更新本工作区，不自动积累历史结果 tabs。当前结果是最近一次成功执行的结果；编辑草稿、运行中、失败时均保留旧结果并明确显示状态。错误持续可见；成功返回零行也替换旧结果。
- **会话范围：**退出 parqx 后不恢复工作区。
- **执行规则：**全应用一次只执行一条 SQL，不排队；运行中可切换 tabs、浏览结果、编辑其他工作区，运行中的工作区只读。第一版没有中途取消，不允许关闭正在运行的工作区。
- **数据源范围：**所有工作区可查询本次打开且已加载成功的全部文件。关闭文件 tab 不移除数据源；查询结果不自动变成可查询表。
- **新建入口：**命令面板条目名称为 `Query`。每次创建空白的 `Query N` 工作区并聚焦编辑器，无论发起时处于文件 tab 还是查询 tab，都不预填 SQL，也不自动执行。
- **编辑区：**正常尺寸下初始约 8 行，结果占余下空间；长 SQL 在编辑区内部滚动。允许手动调整、收起，小终端适当缩小，各工作区保留自身布局。
- **左侧栏：**全局共享，在文件 tab 和查询工作区中都可使用，默认隐藏，通过命令面板 `Toggle Sources` 开关。供用户查看可查询的文件、表名、列类型，不向 SQL 编辑器插入文本。左右键展开或收起字段结构；在文件项按 Enter，已打开则切换过去，已关闭则重新打开原始文件 TablePane，不创建重复 tab。重开的文件从默认表格视图开始。
- **执行快捷键：**只使用 `Ctrl+Enter`，不设置 `F5` 执行。
- **焦点与放大偏好：**使用 Textual 原生 `Shift+Tab` 焦点导航，撤下 `F2/F3` 专用焦点快捷键；优先复用原生放大机制，不设置自定义 `F10`。
- **关闭体验：**统一使用 `Ctrl+W` 关闭当前 tab，无论焦点位于编辑器、结果还是侧栏；编辑器中不再将它解释为删词。运行中的查询工作区仍禁止关闭。
- **关闭确认：**有 SQL 草稿或查询结果的工作区，使用模态框确认丢弃；空白查询工作区和原始文件 tab 直接关闭。确认只用于关闭，不将 SQL 编辑器改回弹窗。
- **空白状态：**允许关闭最后一个 tab。应用保持打开，提供“新建查询 / 打开数据源”入口；数据源仍保留在侧栏中，可重开文件。

架构层面的记录见 [工作区归属](../adr/0001-query-workspace-ownership.md) 和 [执行与数据源范围](../adr/0002-query-execution-and-source-scope.md)。

### Textual 能力核实

基于项目锁定且本地安装的 Textual 8.2.7，已用临时界面核实以下行为，未修改产品代码：

- 原生命令面板提供 `Maximize / Minimize`，`Escape` 恢复。默认放大会优先选中可放大的祖先；本项目需要排除整个 `TabbedContent`，让目标落在编辑器或结果表格。原生最大化会暂时隐藏 tabs 和侧栏，保留 Footer。
- 原生 `Shift+Tab` 是逆序焦点导航。最小布局依次包含 tabs 栏、编辑器、结果；侧栏展开后也进入焦点循环。它不是编辑器与结果之间的专用切换键。代码编辑器中的 `Tab` 保留缩进语义。
- Textual 没有原生关闭 tab 快捷键。当前 `Ctrl+W` 在 TextArea 中是向左删词，在 parqx 表格中是关闭当前 tab。应用优先级绑定可以将关闭动作统一到各个焦点位置；用户已选择保留此键并统一关闭语义。
- 现有目录保留文件身份、路径、SQL 表名和加载状态，但不保留关闭 TablePane 后的列结构。侧栏需要独立持有轻量结构信息；重新打开文件需要创建新的 TablePane。

依据：[Textual 焦点导航](https://textual.textualize.io/guide/input/)、[原生放大 API](https://textual.textualize.io/api/screen/#textual.screen.Screen.maximize)，以及本地安装包和临时界面实测。

### 关闭确认框

确认框点名将关闭的工作区，例如 `Close Query 2?`，并说明 SQL 草稿和查询结果将被丢弃。提供 `Cancel` 和 `Close` 两个操作，默认焦点在 `Cancel`，`Escape` 等同取消并返回原工作区。取消不改变 SQL、结果或当前布局。

### 建议的落地顺序

1. 先用真实 Textual 界面验证工作区布局：约 8 行编辑区、结果区、隐藏侧栏、原生焦点与放大、关闭确认框，以及零 tab 的空白状态。重点观察 80×24 和 120×40 两种尺寸，确定调整高度与收起控件的具体呈现。
2. 接入查询工作区的 SQL 草稿、当前结果和执行状态，再接入数据源侧栏与文件重开。复用现有查询引擎和表格操作。
3. 验证旧结果保留、失败和零行成功、全局单次执行、运行中关闭受限、编辑器内快捷键，以及关闭全部 tabs 后重开文件的完整流程。

早期 HTML 布局草图仅表达整体方向，其中的快捷键和侧栏插入交互不再代表当前方案。下一版原型应按此确认记录更新。
