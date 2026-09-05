# 中证指数本地数据中心

这是一个 Windows 本地程序，用来把中证指数官网的指数数据抓到本机 SQLite 数据库，再离线筛选、排名并导出 Excel。

## 双击使用

把发布目录放在 E、D、F 等非 C 盘目录，例如 `E:\GPT\中证指数本地数据中心`。双击 `中证指数本地数据中心.exe` 启动图形界面。程序首次启动会在 EXE 同级目录创建：

| 路径 | 用途 |
|---|---|
| `config.json` | 本机配置 |
| `data\csindex.db` | SQLite 数据库 |
| `data\logs` | 运行日志 |
| `exports` | Excel 导出目录 |

程序会拒绝安装或运行在本机 C 盘，避免把长期数据和抓取状态放进系统盘。

## 抓取范围

图形界面提供三个常用范围，也可以直接在范围框中输入任意正整数，例如 `50`、`1500` 或 `2500`：

| 范围 | 含义 |
|---|---|
| `1000` | 官网有效指数列表前 1000 个 |
| `2000` | 官网有效指数列表前 2000 个 |
| `全部` | 当前官网有效指数全部抓取 |

固定范围首次创建后会保存成员和顺序。后续官网顺序变化不会自动漂移。

## 数据口径

每个指数保存完整九项指标：

| 分类 | 指标 |
|---|---|
| 阶段性收益 | 近一月、近三月、年初至今 |
| 年化收益 | 近一年、近三年、近五年 |
| 风险 | 近一年、近三年、近五年年化波动率 |

官网返回空值时，本地数据库保存为空，Excel 显示 `--`，程序不会估算或补造数据。历史快照按 `指数代码 + 数据日期` 保存，不覆盖旧交易日。失败响应原文和每个接口最近两次成功响应也保存在 SQLite 中，便于排障。

## 耗时预期

详情接口按单线程安全限速执行：每次请求间隔 7 到 8 秒，每 25 次请求休息 150 秒。一个指数通常需要收益率和波动率两次详情请求，所以：

| 范围 | 详情请求量 | 首轮耗时说明 |
|---|---:|---|
| `1000` | 约 2000 次 | 通常需要数小时 |
| `2000` | 约 4000 次 | 通常需要更久，建议保持电脑不断电 |
| `全部` | 随官网指数数量变化 | 以实际有效指数数量为准 |

实际耗时会受官网响应、网络、WAF 冷却和失败重试影响，不承诺固定完成时间。

## 暂停、继续和冷却

点击“暂停”后，程序会等当前请求结束，不再领取新任务。点击“继续”可从断点恢复。点击“停止”或关闭窗口时，已完成任务和数据库事务会保留，下次启动可继续。

如果官网返回疑似 WAF 拦截，程序会进入冷却：首次 30 分钟，后续最长 60 分钟。冷却状态写入数据库，关闭后再打开仍会等待剩余时间，不会快速重试。

## Excel 导出

导出的工作簿包含四张表：

| 工作表 | 内容 |
|---|---|
| `完整九项指标` | 范围内全部指数、九项指标、数据日期、缺失数量、完整性 |
| `近一年收益率排名` | 近一年收益率非空记录，按收益率降序 |
| `缺失与异常` | 官网缺失字段、失败任务、日期不一致等 |
| `说明` | 数据源、范围、生成时间和口径说明 |

指数代码按文本写入，保留 `000300` 这类前导零。中国市场配色按上涨红色、下跌绿色。

## CLI 排障

开发环境或发布目录中可用命令：

```powershell
csindex-local init
csindex-local status --json
csindex-local crawl --scope 2000 --mode missing
csindex-local crawl --scope all --mode update
csindex-local export --scope fixed:2000 --sort one_year --limit all
csindex-local reset-running
```

常见退出码：

| 退出码 | 含义 |
|---:|---|
| `2` | 配置或参数错误 |
| `3` | 网络准备失败、官网阻断或响应异常 |
| `4` | 数据库或运行错误 |
| `130` | 用户停止 |

## 真实接口冒烟

`scripts\smoke_test.ps1` 会访问真实中证指数官网，只能显式选择 `1`、`20`、`100` 三档，默认只跑 `1`，不会自动升级档位。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\smoke_test.ps1 -Count 1
powershell -ExecutionPolicy Bypass -File scripts\smoke_test.ps1 -Count 20
powershell -ExecutionPolicy Bypass -File scripts\smoke_test.ps1 -Count 100
```

遇到 WAF 冷却或异常时，停止升级档位，保留数据库断点。

## 升级

升级程序时，保留旧目录中的 `config.json`、`data` 和 `exports`，替换 EXE、README 和示例配置即可。不要删除 `data\csindex.db`，否则历史快照、任务状态、原始响应记录都会丢失。

## 构建

构建需要 Windows、Python 3.12、Nuitka 2.6.6 或兼容版本、Tkinter 和 openpyxl。当前环境存在全局 editable `.pth` 中文路径触发 GBK 异常的问题，构建脚本使用 `py -3.12 -S -m nuitka` 加显式 `PYTHONPATH` 绕过，不修改全局 `.pth`。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

构建临时目录和缓存放在项目同级的 E 盘 ASCII 工作区 `csindex-local-nuitka-work`，成品放到项目的 `dist`、`release`。脚本只递归清理经过路径校验的 ASCII 临时工作区，不删除 `data`、`exports`、项目根目录或已有发布目录。
