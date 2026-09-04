# 中证指数本地数据中心设计规格

## 1. 目标

将现有 `csindex_crawl.py` 从“筛选投资标的指数并导出前 100 名”的一次性脚本，升级为可双击运行的 Windows 本地程序。

程序应能从中证指数官网选择并抓取 1000、2000 或全部指数的完整九项指标，把结果持续保存到本机 SQLite 数据库，并可随时离线筛选、排名和导出 Excel。程序必须支持长时间运行、暂停、断点续传和 WAF 冷却恢复。

## 2. 交付形态

最终交付目录：

```text
E:\GPT\中证指数本地数据中心\
├─ 中证指数本地数据中心.exe
├─ config.json
├─ data\
│  ├─ csindex.db
│  ├─ raw\
│  └─ logs\
├─ exports\
├─ src\
├─ tests\
└─ README.md
```

主入口为 Windows 图形界面 EXE，同时保留命令行入口，便于自动化和排障。程序首次启动自动创建目录和数据库，不要求用户安装数据库服务。

## 3. 范围

### 3.1 包含

- 同步官网全部指数基础信息，当前约 3000 条。
- 抓取范围可选 1000、2000、全部或指定指数代码。
- 对范围内每个指数抓取六项收益率和三项年化波动率。
- SQLite 本地持久化、历史快照、任务状态和原始响应留存。
- 单线程全局限速、批次休息、WAF 冷却、断点续传。
- GUI 查看进度、开始、暂停、继续、停止和导出。
- 离线导出完整数据、收益率排行榜及失败明细。
- Windows EXE 打包和可重复构建脚本。

### 3.2 不包含

- 多 IP、代理池、验证码绕过或任何规避官网访问控制的能力。
- 多线程并发抓取详情接口。
- 对官网缺失的三年或五年历史数据进行估算或伪造。
- 云端数据库、多人协作和公网 Web 服务。

## 4. 数据口径

每个指数保存以下九项指标：

| 分类 | 指标 | 接口字段 |
|---|---|---|
| 阶段性收益 | 近一月收益率 | `oneMonth` |
| 阶段性收益 | 近三月收益率 | `threeMonth` |
| 阶段性收益 | 年初至今收益率 | `thisYear` |
| 年化收益 | 近一年收益率 | `oneYear` |
| 年化收益 | 近三年收益率 | `threeYear` |
| 年化收益 | 近五年收益率 | `fiveYear` |
| 风险 | 近一年年化波动率 | `oneYearNianHua` |
| 风险 | 近三年年化波动率 | `threeYearNianHua` |
| 风险 | 近五年年化波动率 | `fiveYearNianHua` |

数值以百分数数值保存，例如接口返回 `12.04` 时数据库保存 `12.04`，Excel 显示为 `12.04`，不转换成 `0.1204`。

官网返回空值时数据库保存 `NULL`，Excel 显示 `--`，并在完整性字段中标明缺失数量。只有九项均非空的记录才标记为“完整”；新指数因成立年限不足导致的缺失属于官网数据限制，不作为抓取失败。

## 5. 系统架构

```text
GUI / CLI
   │
   ├── 数据同步服务
   │      ├── 官网客户端
   │      ├── 全局限速器
   │      └── 抓取任务状态机
   │
   ├── SQLite 数据仓库
   │      ├── 指数主数据
   │      ├── 指标历史快照
   │      ├── 原始响应
   │      └── 任务与运行记录
   │
   └── 查询与 Excel 导出服务
```

界面层不直接访问网络或数据库，只调用应用服务。官网访问、持久化、任务调度和导出相互独立，可分别测试和替换。

## 6. 模块边界

| 模块 | 职责 |
|---|---|
| `config.py` | 读取、校验并保存配置，提供安全默认值 |
| `models.py` | 定义指数、指标快照、抓取任务等领域数据结构 |
| `db.py` | SQLite 建库、迁移、事务及仓储接口 |
| `csindex_client.py` | 封装三个官网接口和响应解析，不负责重试策略 |
| `rate_limiter.py` | 全局请求间隔、批次休息和冷却计时 |
| `task_queue.py` | 建立任务、领取任务、更新状态、恢复中断任务 |
| `crawler.py` | 协调客户端、限速器、任务队列和数据库 |
| `query_service.py` | 本地筛选、排序、完整性统计和数据日期查询 |
| `excel_exporter.py` | 从数据库导出完整表、排行榜、说明和异常明细 |
| `gui.py` | Windows 图形界面及后台线程通信 |
| `cli.py` | `init`、`crawl`、`status`、`export`、`reset-running` 命令 |
| `main.py` | GUI 启动入口及全局异常处理 |

## 7. SQLite 数据模型

### 7.1 `indices`

- `index_code TEXT PRIMARY KEY`
- `index_name TEXT NOT NULL`
- `if_tracked TEXT`
- `index_series TEXT`
- `index_classify TEXT`
- `assets_classify TEXT`
- `publish_date TEXT`
- `list_order INTEGER NOT NULL`
- `is_active INTEGER NOT NULL DEFAULT 1`
- `raw_json TEXT NOT NULL`
- `synced_at TEXT NOT NULL`

### 7.2 `crawl_scopes`

- `id TEXT PRIMARY KEY`，固定范围使用 `fixed:1000`、`fixed:2000`、`fixed:all`
- `name TEXT NOT NULL`
- `scope_type TEXT NOT NULL`
- `scope_value TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

### 7.3 `crawl_scope_members`

- `scope_id TEXT NOT NULL`
- `index_code TEXT NOT NULL`
- `member_order INTEGER NOT NULL`
- `added_at TEXT NOT NULL`
- `PRIMARY KEY(scope_id, index_code)`
- 外键分别关联 `crawl_scopes.id` 和 `indices.index_code`

固定范围首次创建后保留成员和顺序；只有用户明确执行“重新生成范围”时才替换成员。

### 7.4 `metric_snapshots`

- `index_code TEXT NOT NULL`
- `data_date TEXT NOT NULL`
- 九项指标，类型均为 `REAL NULL`
- `yield_fetched_at TEXT NULL`
- `volatility_fetched_at TEXT NULL`
- `missing_count INTEGER NOT NULL DEFAULT 9`
- `is_complete INTEGER NOT NULL DEFAULT 0`
- `PRIMARY KEY(index_code, data_date)`
- 外键关联 `indices.index_code`

收益率接口成功后先写入六项及 `data_date`；波动率接口成功后合并同一快照。写入操作必须使用事务和 UPSERT。

### 7.5 `raw_responses`

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `index_code TEXT`
- `endpoint TEXT NOT NULL`
- `http_status INTEGER NOT NULL`
- `data_date TEXT`
- `payload TEXT`
- `fetched_at TEXT NOT NULL`

仅保留最近两轮成功响应和全部失败响应，防止数据库无限增长。

### 7.6 `crawl_runs`

- `id TEXT PRIMARY KEY`
- `scope_type TEXT NOT NULL`
- `scope_value TEXT NOT NULL`
- `target_data_date TEXT`
- `status TEXT NOT NULL`
- `started_at TEXT NOT NULL`
- `finished_at TEXT`
- `total_tasks INTEGER NOT NULL`
- `success_tasks INTEGER NOT NULL DEFAULT 0`
- `failed_tasks INTEGER NOT NULL DEFAULT 0`

### 7.7 `crawl_tasks`

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `run_id TEXT NOT NULL`
- `index_code TEXT NOT NULL`
- `endpoint TEXT NOT NULL`
- `target_data_date TEXT`
- `status TEXT NOT NULL`
- `attempts INTEGER NOT NULL DEFAULT 0`
- `available_at TEXT NOT NULL`
- `last_http_status INTEGER`
- `last_error TEXT`
- `updated_at TEXT NOT NULL`
- 唯一约束：`run_id, index_code, endpoint`

任务状态限定为：`pending`、`running`、`success`、`retry_wait`、`blocked_wait`、`failed`、`cancelled`。

程序启动时，将上次异常退出遗留的 `running` 任务恢复为 `pending`。

### 7.8 `runtime_state`

- `key TEXT PRIMARY KEY`
- `value_json TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

保存需要跨进程恢复的少量运行状态。首版至少保存 WAF 冷却截止时间和下一次冷却时长；写入使用 UPSERT，清除冷却时删除对应键。该表不保存用户的指数指标数据。

## 8. 抓取流程

### 8.1 启动检查

1. 加载并校验配置。
2. 初始化或迁移数据库。
3. 恢复遗留的运行中任务。
4. 同步全部指数基础列表。
5. 请求基准指数 `000300` 的收益率，读取官网最新 `endDate`。
6. 根据用户范围和本地快照创建缺失或过期任务。

### 8.2 范围规则

- `1000`、`2000`：按官网列表稳定顺序选择前 N 个有效指数代码。
- `全部`：选择当前全部有效指数。
- `指定代码`：读取用户输入或文本文件中的指数代码。
- 后续版本可增加按指数系列或资产分类筛选，但首版不增加复杂筛选界面。

为避免“官网顺序变化导致范围漂移”，首次建立范围后把选中代码保存到本地；用户主动点击“重新生成范围”才更新成员。

### 8.3 任务顺序

同一指数优先连续完成收益率和波动率任务，使单条记录尽快完整。整体任务按以下优先级处理：

1. 用户指定指数。
2. 已完成收益率但缺少波动率的指数。
3. 完全未抓取的指数。
4. 已有旧日期、需要更新的指数。

### 8.4 限速和冷却

- 详情接口全局单线程。
- 两次请求之间随机等待 7.0～8.0 秒。
- 每成功或失败发出 25 个请求后休息 150 秒。
- 403/404 被识别为 WAF 阻断时，不立即重试当前任务。
- 首次阻断进入 30 分钟冷却，只在冷却结束后发送一次基准探测。
- 探测仍被阻断时，下一次冷却为 60 分钟；之后维持 60 分钟间隔。
- 用户关闭程序后，冷却截止时间保存到数据库；再次启动仍需等待剩余冷却时间。
- 网络超时使用 30、120、300 秒退避，最多三次；超过后标记 `failed`，不影响其他任务。

### 8.5 暂停与退出

- 点击暂停后，不再领取新任务；当前 HTTP 请求完成后进入暂停状态。
- 点击停止后，安全提交当前事务并把未执行任务保留为 `pending`。
- 窗口关闭时执行与停止相同的安全退出逻辑。
- 数据库启用 WAL 模式和 `busy_timeout`，GUI 查询不阻塞抓取写入。

## 9. 更新策略

### 首次运行

创建选定范围并抓取所有缺失接口，完成一个可离线使用的完整快照。1000 个指数约 2000 次详情请求，2000 个指数约 4000 次详情请求。

### 后续运行

- 每次启动都同步基础列表并检查最新交易日。
- 本地最新日期等于官网日期时，不创建重复详情任务。
- 官网日期更新后，为选定范围创建新日期的收益率任务。
- 波动率结果跟随同一数据日期创建任务。
- 历史快照保留，不覆盖旧交易日。

### 数据日期异常

如果某个指数收益率接口返回的 `endDate` 与本轮目标日期不同，仍保存真实日期，但任务标记为“日期不一致”，不把该记录计入本轮完整数量。导出时单独列入异常明细。

## 10. GUI 设计

GUI 使用 Python 标准库 Tkinter/ttk，避免引入大型 GUI 依赖。

主窗口包含：

- 抓取范围：1000、2000、全部、指定代码。
- 更新模式：补齐缺失、本交易日更新、强制重新抓取。
- 安全参数显示：请求间隔、批次数、批次休息；默认折叠，修改低于安全下限时拒绝保存。
- 开始、暂停、继续、停止、导出 Excel。
- 总任务、成功、待处理、失败、数据完整记录数。
- 当前指数、当前接口、最近 HTTP 状态、冷却剩余时间和预计剩余时间。
- 滚动日志区，仅显示摘要；完整日志写入文件。

抓取在后台工作线程中运行，通过线程安全队列向 GUI 推送状态。所有 Tkinter 控件只在主线程更新。

## 11. 命令行接口

```text
csindex-local init
csindex-local crawl --scope 2000 --mode missing
csindex-local crawl --scope all --mode update
csindex-local status
csindex-local export --scope 2000 --sort one_year --limit all
csindex-local export --scope 2000 --sort one_year --limit 100
```

GUI 与 CLI 复用相同的应用服务，不能维护两套抓取逻辑。

## 12. Excel 输出

完整数据工作簿至少包含：

1. `完整九项指标`：范围内全部指数，排名、代码、名称、九项指标、数据日期、缺失项数、完整性状态。
2. `近一年收益率排名`：只包含近一年收益率非空的记录，严格降序。
3. `缺失与异常`：官网缺失字段、请求失败、日期不一致记录。
4. `说明`：数据源、抓取范围、数据日期、生成时间、缺失值口径和程序版本。

指数代码必须按文本写入，保留 `000300` 等前导零。数值按两位小数写入，保持为可排序的数值单元格。中国市场配色采用上涨红色、下跌绿色。

## 13. 配置

`config.json` 默认值：

```json
{
  "data_dir": "E:\\GPT\\中证指数本地数据中心\\data",
  "export_dir": "E:\\GPT\\中证指数本地数据中心\\exports",
  "request_delay_min_seconds": 7.0,
  "request_delay_max_seconds": 8.0,
  "batch_size": 25,
  "batch_rest_seconds": 150,
  "blocked_initial_cooldown_seconds": 1800,
  "blocked_max_cooldown_seconds": 3600,
  "http_timeout_seconds": 20,
  "default_scope": 2000
}
```

任何界面配置都不得允许请求间隔低于 7 秒或批次休息低于 150 秒。

## 14. 错误处理

- 数据库写入失败：停止抓取，保留任务状态并显示明确错误，不继续发请求。
- 列表接口失败：本轮不创建新范围；已有数据库仍可离线查询和导出。
- JSON 格式异常：保存原始响应并将任务延后重试。
- 单个指数不存在：记录 404；只有返回拦截页特征时才判定为 WAF，普通业务 404 标记为失败。
- 磁盘空间不足：停止抓取并提示目标目录。
- Excel 正被占用：输出带时间戳的新文件，不覆盖或破坏已有文件。
- 强制退出：下次启动恢复 `running` 任务，不重复损坏数据。

## 15. 测试策略

### 单元测试

- 九项字段解析、空值及异常 JSON。
- SQLite UPSERT、历史日期隔离、事务回滚。
- 任务状态转换和异常退出恢复。
- 限速、批次休息、WAF 冷却和网络退避，使用虚拟时钟，不真实等待。
- 范围固定和重新生成逻辑。
- 排名、缺失统计和日期一致性检查。
- Excel 中指数代码、数值类型和工作表内容。

### 集成测试

- 使用本地伪造 HTTP 服务模拟 200、403、404、超时和坏 JSON。
- 完成 20 个指数的端到端建库和导出。
- 中途停止并重启，验证不会重复已成功任务。

### 真实接口验证

依次执行 1、20、100 条真实数据验证。只有前三轮无异常后，才允许用户选择 1000、2000 或全部范围。真实接口测试始终遵守正式限速。

## 16. 打包

- 开发环境以当前可用的 Python 3.9+ 为基线。
- 首选 Nuitka standalone 打包为 Windows EXE；若 GUI 或依赖兼容性阻塞，再评估 PyInstaller。
- 打包时包含数据库迁移文件、默认配置和应用图标，不包含测试缓存和用户数据库。
- EXE 首次启动在程序目录创建可写数据目录；若目录不可写，明确提示用户选择新位置。

## 17. 验收标准

- 双击 EXE 能启动 GUI 并创建数据库。
- 能建立 1000、2000 和全部三种固定抓取范围。
- 每个选中指数都创建收益率和波动率任务，不再受前 100 排名限制。
- 程序关闭重启后能从断点继续，成功任务不重复抓取。
- 403/404 拦截不会产生快速重试。
- 数据跨交易日保存为不同快照。
- 完整九项指标有值的记录全部正确写入；官网缺失值被明确标注。
- Excel 包含四个约定工作表，指数代码前导零、排序和数值类型正确。
- 单元测试和伪 HTTP 集成测试通过。
- 20 条、100 条真实接口验证通过后才开放大规模任务。

## 18. 实施顺序

1. 建立项目骨架、配置模型和测试框架。
2. 实现 SQLite 数据模型、迁移和仓储。
3. 实现官网客户端及严格响应分类。
4. 实现持久化任务队列、限速和冷却状态机。
5. 实现抓取协调器和增量更新策略。
6. 实现本地查询与 Excel 导出。
7. 实现 CLI。
8. 实现 Tkinter GUI。
9. 完成模拟接口集成测试。
10. 按 1、20、100 条执行真实接口验证。
11. 打包 Windows EXE并完成干净机器验证。
