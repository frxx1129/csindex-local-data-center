# 中证指数本地数据中心 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个可双击运行、可断点续传、能将 1000/2000/全部指数的九项指标保存到 SQLite 并导出 Excel 的 Windows 本地程序。

**Architecture:** 使用分层 Python 应用：官网客户端只解析 HTTP，持久化任务队列负责状态，抓取协调器串联全局限速器与 SQLite，GUI/CLI 复用同一应用服务。所有长任务在后台工作线程运行，Excel 只从本地数据库生成。

**Tech Stack:** Python 3.12、标准库 `sqlite3/urllib/tkinter/argparse`、openpyxl、pytest、Nuitka standalone。

**Spec:** `docs/superpowers/specs/2026-09-04-csindex-local-data-center-design.md`

## Global Constraints

- 输出根目录固定为 `E:\GPT\中证指数本地数据中心`。
- 详情接口只能全局单线程请求，请求间隔必须为 7.0～8.0 秒。
- 每 25 次请求后必须休息 150 秒。
- 403/404 拦截不得快速重试；首次冷却 1800 秒，后续最长 3600 秒。
- 每个指数必须创建收益率和年化波动率两个任务，不受前 100 排名限制。
- SQLite 按 `index_code + data_date` 保存历史快照，不覆盖旧交易日。
- 官网空值保存为 `NULL`，导出显示 `--`，不得估算。
- 指数代码按文本处理，必须保留前导零。
- 中国市场配色：上涨红色，下降绿色。

---

### Task 1: 项目骨架与配置系统

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `src/csindex_local/__init__.py`
- Create: `src/csindex_local/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `AppConfig.load(path: Path) -> AppConfig`
- Produces: `AppConfig.save(path: Path) -> None`
- Produces: `AppConfig.ensure_directories() -> None`

- [ ] **Step 1: 初始化 Git 和项目目录**

Run:

```powershell
Set-Location 'E:\GPT\中证指数本地数据中心'
git init
New-Item -ItemType Directory -Force src\csindex_local, tests, data, exports, scripts | Out-Null
```

Expected: `git status` 显示一个空的新仓库，项目目录均存在。

- [ ] **Step 2: 编写配置失败测试**

```python
from pathlib import Path
import pytest

from csindex_local.config import AppConfig


def test_default_config_uses_safe_limits(tmp_path: Path):
    config = AppConfig.default(tmp_path)
    assert config.request_delay_min_seconds == 7.0
    assert config.request_delay_max_seconds == 8.0
    assert config.batch_size == 25
    assert config.batch_rest_seconds == 150
    assert config.default_scope == "2000"


def test_rejects_unsafe_rate_limit(tmp_path: Path):
    config = AppConfig.default(tmp_path)
    config.request_delay_min_seconds = 6.9
    with pytest.raises(ValueError, match="请求间隔不能低于 7 秒"):
        config.validate()


def test_save_and_load_round_trip(tmp_path: Path):
    path = tmp_path / "config.json"
    expected = AppConfig.default(tmp_path)
    expected.save(path)
    assert AppConfig.load(path) == expected
```

- [ ] **Step 3: 运行配置测试并确认失败**

Run:

```powershell
python -m pytest tests\test_config.py -v
```

Expected: FAIL，提示 `csindex_local.config` 不存在。

- [ ] **Step 4: 实现最小配置模型**

```python
from dataclasses import asdict, dataclass
import json
from pathlib import Path


@dataclass
class AppConfig:
    data_dir: str
    export_dir: str
    request_delay_min_seconds: float = 7.0
    request_delay_max_seconds: float = 8.0
    batch_size: int = 25
    batch_rest_seconds: int = 150
    blocked_initial_cooldown_seconds: int = 1800
    blocked_max_cooldown_seconds: int = 3600
    http_timeout_seconds: int = 20
    default_scope: str = "2000"

    @classmethod
    def default(cls, root: Path) -> "AppConfig":
        return cls(str(root / "data"), str(root / "exports"))

    def validate(self) -> None:
        if self.request_delay_min_seconds < 7.0:
            raise ValueError("请求间隔不能低于 7 秒")
        if self.request_delay_max_seconds < self.request_delay_min_seconds:
            raise ValueError("最大请求间隔不能小于最小请求间隔")
        if self.batch_size > 25 or self.batch_size < 1:
            raise ValueError("批次大小必须在 1 到 25 之间")
        if self.batch_rest_seconds < 150:
            raise ValueError("批次休息不能低于 150 秒")

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        value = cls(**json.loads(path.read_text(encoding="utf-8")))
        value.validate()
        return value

    def save(self, path: Path) -> None:
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    def ensure_directories(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.export_dir).mkdir(parents=True, exist_ok=True)
        (Path(self.data_dir) / "logs").mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 5: 添加构建元数据并运行测试**

`pyproject.toml` 定义 `src` 布局、Python `>=3.12`、pytest 测试路径以及 `csindex-local = csindex_local.cli:main`。`requirements.txt` 固定 `openpyxl>=3.1,<4` 和 `pytest>=8,<9`。

Run:

```powershell
python -m pip install -e .
python -m pytest tests\test_config.py -v
```

Expected: 3 tests PASS。

- [ ] **Step 6: 提交配置基础**

```powershell
git add .gitignore pyproject.toml requirements.txt src tests
git commit -m "feat: scaffold local data center configuration"
```

---

### Task 2: SQLite 数据模型与仓储

**Files:**
- Create: `src/csindex_local/models.py`
- Create: `src/csindex_local/db.py`
- Create: `tests/test_db.py`

**Interfaces:**
- Produces: `Database(path: Path)`
- Produces: `Database.initialize() -> None`
- Produces: `Database.upsert_indices(items: list[IndexRecord]) -> None`
- Produces: `Database.upsert_yield(snapshot: YieldSnapshot) -> None`
- Produces: `Database.merge_volatility(snapshot: VolatilitySnapshot) -> None`
- Produces: `Database.recover_interrupted_tasks() -> int`
- Produces: `Database.create_or_get_scope(scope_id: str, codes: list[str]) -> CrawlScope`

- [ ] **Step 1: 编写数据库失败测试**

```python
from pathlib import Path

from csindex_local.db import Database
from csindex_local.models import IndexRecord, YieldSnapshot, VolatilitySnapshot


def test_snapshot_is_merged_by_code_and_date(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.upsert_indices([IndexRecord("000300", "沪深300", "是", {})])
    db.upsert_yield(YieldSnapshot("000300", "2026-09-03", 1, 2, 3, 4, 5, 6))
    db.merge_volatility(VolatilitySnapshot("000300", "2026-09-03", 7, 8, 9))
    row = db.get_snapshot("000300", "2026-09-03")
    assert row["one_year"] == 4
    assert row["five_year_volatility"] == 9
    assert row["missing_count"] == 0
    assert row["is_complete"] == 1


def test_different_dates_do_not_overwrite(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.upsert_indices([IndexRecord("000300", "沪深300", "是", {})])
    db.upsert_yield(YieldSnapshot("000300", "2026-09-02", 1, 2, 3, 4, 5, 6))
    db.upsert_yield(YieldSnapshot("000300", "2026-09-03", 2, 3, 4, 5, 6, 7))
    assert len(db.list_snapshots("000300")) == 2
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests\test_db.py -v`

Expected: FAIL，缺少 `db` 和 `models` 模块。

- [ ] **Step 3: 定义不可变领域模型**

```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IndexRecord:
    index_code: str
    index_name: str
    if_tracked: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class YieldSnapshot:
    index_code: str
    data_date: str
    one_month: float | None
    three_month: float | None
    year_to_date: float | None
    one_year: float | None
    three_year: float | None
    five_year: float | None


@dataclass(frozen=True)
class VolatilitySnapshot:
    index_code: str
    data_date: str
    one_year: float | None
    three_year: float | None
    five_year: float | None


@dataclass(frozen=True)
class CrawlScope:
    id: str
    name: str
    scope_type: str
    scope_value: str
    codes: tuple[str, ...]
```

- [ ] **Step 4: 实现数据库初始化和事务仓储**

使用 `sqlite3.Row`、`PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON` 和 `PRAGMA busy_timeout=5000`。建表 SQL 必须与规格的 `indices`、`crawl_scopes`、`crawl_scope_members`、`metric_snapshots`、`raw_responses`、`crawl_runs`、`crawl_tasks` 一致。每次合并快照后，用 SQL 统计九个指标中的 `NULL` 数量并更新 `missing_count/is_complete`。范围创建必须在一个事务内写入范围及有序成员；已有范围默认原样返回。

核心合并 SQL：

```sql
INSERT INTO metric_snapshots (
  index_code, data_date, one_month, three_month, year_to_date,
  one_year, three_year, five_year, yield_fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(index_code, data_date) DO UPDATE SET
  one_month=excluded.one_month,
  three_month=excluded.three_month,
  year_to_date=excluded.year_to_date,
  one_year=excluded.one_year,
  three_year=excluded.three_year,
  five_year=excluded.five_year,
  yield_fetched_at=excluded.yield_fetched_at;
```

- [ ] **Step 5: 运行数据库测试**

Run: `python -m pytest tests\test_db.py -v`

Expected: PASS，且测试目录没有遗留打开的数据库句柄。

- [ ] **Step 6: 提交数据库层**

```powershell
git add src\csindex_local\models.py src\csindex_local\db.py tests\test_db.py
git commit -m "feat: add sqlite snapshot repository"
```

---

### Task 3: 中证官网客户端与严格响应分类

**Files:**
- Create: `src/csindex_local/csindex_client.py`
- Create: `tests/test_client.py`
- Create: `tests/fixtures/yield_ok.json`
- Create: `tests/fixtures/volatility_ok.json`

**Interfaces:**
- Produces: `CsindexClient.fetch_index_list() -> list[IndexRecord]`
- Produces: `CsindexClient.fetch_yield(code: str) -> YieldSnapshot`
- Produces: `CsindexClient.fetch_volatility(code: str, data_date: str) -> VolatilitySnapshot`
- Produces exceptions: `BlockedError`, `NotFoundError`, `NetworkError`, `ResponseFormatError`

- [ ] **Step 1: 编写解析和错误分类测试**

```python
import pytest

from csindex_local.csindex_client import BlockedError, CsindexClient, NotFoundError


def test_parse_yield_preserves_code_and_nulls():
    payload = {"code": "200", "data": {
        "indexCode": "000300", "endDate": "2026-09-03",
        "oneMonth": "1.25", "threeMonth": "--", "thisYear": "3.5",
        "oneYear": "4", "threeYear": "5", "fiveYear": "6"
    }}
    row = CsindexClient.parse_yield(payload)
    assert row.index_code == "000300"
    assert row.three_month is None


def test_html_block_page_is_blocked():
    with pytest.raises(BlockedError):
        CsindexClient.classify_http_error(404, b"<html>\xe6\x82\xa8\xe7\x9a\x84\xe8\xae\xbf\xe9\x97\xae\xe8\xa2\xab\xe9\x98\xbb\xe6\x96\xad</html>")


def test_plain_business_404_is_not_found():
    with pytest.raises(NotFoundError):
        CsindexClient.classify_http_error(404, b'{"message":"not found"}')
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests\test_client.py -v`

Expected: FAIL，缺少客户端模块。

- [ ] **Step 3: 实现客户端**

实现浏览器 UA、逐指数 Referer、分页列表 POST、两类详情 GET 和百分数字符串解析。`urllib.error.HTTPError` 的响应体必须被读取后分类，不能把所有 404 都当成 WAF。

```python
def parse_number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    if text in {"", "--", "-", "null", "None"}:
        return None
    return float(text)
```

- [ ] **Step 4: 运行客户端测试**

Run: `python -m pytest tests\test_client.py -v`

Expected: PASS，不访问真实官网。

- [ ] **Step 5: 提交官网客户端**

```powershell
git add src\csindex_local\csindex_client.py tests\test_client.py tests\fixtures
git commit -m "feat: add strict csindex http client"
```

---

### Task 4: 持久化任务队列

**Files:**
- Modify: `src/csindex_local/models.py`
- Modify: `src/csindex_local/db.py`
- Create: `src/csindex_local/task_queue.py`
- Create: `tests/test_task_queue.py`

**Interfaces:**
- Produces: `TaskQueue.create_run(scope_type, scope_value, codes, target_date) -> str`
- Produces: `TaskQueue.claim_next(run_id) -> CrawlTask | None`
- Produces: `TaskQueue.mark_success(task_id) -> None`
- Produces: `TaskQueue.mark_retry(task_id, available_at, error) -> None`
- Produces: `TaskQueue.mark_blocked(task_id, available_at, status) -> None`
- Produces: `TaskQueue.progress(run_id) -> RunProgress`

在 `models.py` 中增加任务返回类型：

```python
@dataclass(frozen=True)
class CrawlTask:
    id: int
    run_id: str
    index_code: str
    endpoint: str
    target_data_date: str | None
    status: str
    attempts: int


@dataclass(frozen=True)
class RunProgress:
    total_tasks: int
    success_tasks: int
    failed_tasks: int
    pending_tasks: int
```

- [ ] **Step 1: 编写状态机失败测试**

```python
def test_each_code_gets_two_tasks(queue):
    run_id = queue.create_run("fixed", "2", ["000300", "000905"], "2026-09-03")
    assert queue.progress(run_id).total_tasks == 4


def test_claim_is_atomic_and_recovery_requeues_running(queue):
    run_id = queue.create_run("fixed", "1", ["000300"], "2026-09-03")
    first = queue.claim_next(run_id)
    assert first.status == "running"
    assert queue.recover_interrupted() == 1
    assert queue.claim_next(run_id).id == first.id
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests\test_task_queue.py -v`

Expected: FAIL，缺少任务队列。

- [ ] **Step 3: 实现原子领取和合法状态转换**

用 `BEGIN IMMEDIATE` 领取任务，先选一条 `pending/retry_wait/blocked_wait` 且 `available_at <= now` 的任务，再在同一事务更新为 `running`。拒绝从 `success` 返回 `running`。

任务创建顺序：指定代码、已有收益缺波动率、全新指数、旧日期更新；同一代码 `yield` 在 `volatility` 前。

- [ ] **Step 4: 运行任务队列测试**

Run: `python -m pytest tests\test_task_queue.py -v`

Expected: PASS。

- [ ] **Step 5: 提交任务队列**

```powershell
git add src\csindex_local\models.py src\csindex_local\db.py src\csindex_local\task_queue.py tests\test_task_queue.py
git commit -m "feat: add persistent crawl task queue"
```

---

### Task 5: 全局限速与 WAF 冷却状态机

**Files:**
- Create: `src/csindex_local/rate_limiter.py`
- Create: `tests/test_rate_limiter.py`

**Interfaces:**
- Produces: `RateLimiter.before_request() -> None`
- Produces: `RateLimiter.after_request() -> None`
- Produces: `RateLimiter.enter_blocked_cooldown(now: datetime) -> datetime`
- Produces: `RateLimiter.clear_blocked_cooldown() -> None`

- [ ] **Step 1: 使用虚拟时钟编写失败测试**

```python
def test_25_requests_trigger_150_second_rest(fake_clock, limiter):
    for _ in range(25):
        limiter.before_request()
        limiter.after_request()
    limiter.before_request()
    assert fake_clock.sleeps[-1] >= 150


def test_blocked_cooldown_grows_from_30_to_60_minutes(fake_clock, limiter):
    first = limiter.enter_blocked_cooldown(fake_clock.now())
    fake_clock.advance_to(first)
    second = limiter.enter_blocked_cooldown(fake_clock.now())
    assert (second - fake_clock.now()).total_seconds() == 3600
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests\test_rate_limiter.py -v`

Expected: FAIL，缺少限速器。

- [ ] **Step 3: 实现可注入时钟的限速器**

构造函数接收 `clock`、`random_uniform` 和持久化冷却回调。正常间隔由 `random_uniform(7.0, 8.0)` 决定；批次休息与阻断冷却分开计时。测试不得真实 `sleep`。

- [ ] **Step 4: 运行测试并提交**

Run: `python -m pytest tests\test_rate_limiter.py -v`

Expected: PASS。

```powershell
git add src\csindex_local\rate_limiter.py tests\test_rate_limiter.py
git commit -m "feat: enforce global rate and waf cooldown"
```

---

### Task 6: 抓取协调器与增量更新

**Files:**
- Modify: `src/csindex_local/db.py`
- Modify: `src/csindex_local/models.py`
- Create: `src/csindex_local/crawler.py`
- Create: `tests/test_crawler.py`

**Interfaces:**
- Consumes: `Database`、`CsindexClient`、`TaskQueue`、`RateLimiter`
- Produces: `Crawler.prepare_run(scope: ScopeSelection, mode: UpdateMode) -> str`
- Produces: `Crawler.run(run_id: str, control: CrawlControl, on_event: Callable) -> RunProgress`
- Produces: `CrawlControl.pause/resume/stop`
- Produces: `Database.get_runtime_state(key: str) -> dict | None`
- Produces: `Database.set_runtime_state(key: str, value: dict) -> None`
- Produces: `Database.delete_runtime_state(key: str) -> None`

在 `models.py` 中增加明确的公共类型：

```python
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ScopeSelection:
    kind: str  # fixed_count | all | codes
    value: int | tuple[str, ...]
    regenerate: bool = False


class UpdateMode(str, Enum):
    MISSING = "missing"
    UPDATE = "update"
    FORCE = "force"
```

- [ ] **Step 1: 编写端到端协调失败测试**

```python
def test_two_codes_fetch_all_nine_metrics(fake_client, database, fast_limiter):
    crawler = build_crawler(fake_client, database, fast_limiter)
    run_id = crawler.prepare_run(ScopeSelection("codes", ("000300", "000905")), UpdateMode.MISSING)
    progress = crawler.run(run_id, CrawlControl(), lambda event: None)
    assert progress.success_tasks == 4
    assert database.get_snapshot("000300", "2026-09-03")["is_complete"] == 1


def test_restart_does_not_repeat_successful_task(fake_client, database, fast_limiter):
    crawler = build_crawler(fake_client, database, fast_limiter)
    run_id = crawler.prepare_run(ScopeSelection("codes", ("000300",)), UpdateMode.MISSING)
    crawler.run_one(run_id)
    restarted = build_crawler(fake_client, database, fast_limiter)
    restarted.run(run_id, CrawlControl(), lambda event: None)
    assert fake_client.calls.count(("yield", "000300")) == 1
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests\test_crawler.py -v`

Expected: FAIL，缺少协调器。

- [ ] **Step 3: 实现准备运行逻辑**

准备运行必须：同步指数列表、探测 `000300` 最新日期、固定范围成员、比较本地快照、只为缺失或过期接口创建任务。`FORCE` 模式允许创建新运行重新抓取，但仍保留历史快照。

- [ ] **Step 4: 实现循环、控制信号和错误映射**

```python
while not control.stopped:
    control.wait_if_paused()
    task = queue.claim_next(run_id)
    if task is None:
        break
    limiter.before_request()
    try:
        execute_task(task)
    except BlockedError as exc:
        until = limiter.enter_blocked_cooldown(clock.now())
        queue.mark_blocked(task.id, until, exc.http_status)
    except NetworkError as exc:
        queue.mark_retry(task.id, retry_time(task.attempts), str(exc))
    except (NotFoundError, ResponseFormatError) as exc:
        queue.mark_failed(task.id, str(exc))
    else:
        queue.mark_success(task.id)
    finally:
        limiter.after_request()
```

- [ ] **Step 5: 运行协调器测试**

Run: `python -m pytest tests\test_crawler.py -v`

Expected: PASS，包括暂停、停止、拦截冷却和日期不一致用例。

- [ ] **Step 6: 提交抓取服务**

```powershell
git add src\csindex_local\crawler.py tests\test_crawler.py
git commit -m "feat: orchestrate resumable incremental crawling"
```

---

### Task 7: 本地查询与 Excel 导出

**Files:**
- Create: `src/csindex_local/query_service.py`
- Create: `src/csindex_local/excel_exporter.py`
- Create: `tests/test_export.py`

**Interfaces:**
- Produces: `QueryService.latest_rows(scope_id: str) -> list[MetricRow]`
- Produces: `QueryService.rank(rows, field: str, limit: int | None) -> list[MetricRow]`
- Produces: `ExcelExporter.export(scope_id: str, output_path: Path, ranking_limit: int | None) -> Path`

在 `models.py` 中定义查询结果，九项指标字段名与数据库保持一致：

```python
@dataclass(frozen=True)
class MetricRow:
    index_code: str
    index_name: str
    data_date: str
    one_month: float | None
    three_month: float | None
    year_to_date: float | None
    one_year: float | None
    three_year: float | None
    five_year: float | None
    one_year_volatility: float | None
    three_year_volatility: float | None
    five_year_volatility: float | None
    missing_count: int
    is_complete: bool
```

- [ ] **Step 1: 编写查询和工作簿失败测试**

```python
from openpyxl import load_workbook


def test_export_has_four_sheets_and_text_codes(seed_database, tmp_path):
    seed_database.create_or_get_scope("scope-1", ["000300"])
    path = ExcelExporter(seed_database).export("scope-1", tmp_path / "out.xlsx", 100)
    workbook = load_workbook(path, data_only=False)
    assert workbook.sheetnames == ["完整九项指标", "近一年收益率排名", "缺失与异常", "说明"]
    sheet = workbook["完整九项指标"]
    assert sheet["B3"].value == "000300"
    assert sheet["B3"].number_format == "@"


def test_ranking_excludes_missing_one_year(query_service):
    rows = query_service.rank(query_service.latest_rows("scope-1"), "one_year", None)
    assert all(row.one_year is not None for row in rows)
    assert [r.one_year for r in rows] == sorted([r.one_year for r in rows], reverse=True)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests\test_export.py -v`

Expected: FAIL，缺少查询和导出模块。

- [ ] **Step 3: 实现只读查询服务**

使用窗口函数或最大日期关联查询每个范围成员的最新快照。不得在 Python 中用浮点字符串排序；数据库返回 `REAL`，`NULL` 排在最后并从排行榜排除。

- [ ] **Step 4: 实现四表 Excel 导出**

工作簿要求：冻结窗格、自动筛选、列宽上限、文本代码格式、两位小数、缺失显示 `--`、上涨红字 `C00000`、下降绿字 `008000`。完整表还要包含 `data_date/missing_count/is_complete`。

- [ ] **Step 5: 运行导出测试并人工打开样例**

Run:

```powershell
python -m pytest tests\test_export.py -v
```

Expected: PASS；样例工作簿可由 Excel 打开，四张表存在且表头未被截断。

- [ ] **Step 6: 提交查询和导出**

```powershell
git add src\csindex_local\query_service.py src\csindex_local\excel_exporter.py tests\test_export.py
git commit -m "feat: export local metrics and rankings to excel"
```

---

### Task 8: 命令行入口

**Files:**
- Create: `src/csindex_local/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: 配置、数据库、抓取协调器、查询和导出服务
- Produces: `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: 编写 CLI 失败测试**

```python
def test_status_returns_json(cli_runner, initialized_app):
    result = cli_runner(["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert {"indices", "snapshots", "pending_tasks"} <= payload.keys()


def test_crawl_accepts_2000_scope(cli_runner, initialized_app):
    result = cli_runner(["crawl", "--scope", "2000", "--mode", "missing", "--dry-run"])
    assert result.exit_code == 0
    assert "将创建" in result.stdout
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests\test_cli.py -v`

Expected: FAIL，缺少 CLI。

- [ ] **Step 3: 实现命令和退出码**

支持。GUI 只展示 1000、2000、全部，CLI 为测试和排障允许任意正整数；固定数字范围 ID 统一为 `fixed:<数量>`，全部范围为 `fixed:all`，代码文件范围 ID 为 `codes:<文件内容SHA256前12位>`：

```text
init
crawl --scope <正整数>|all|codes.txt --mode missing|update|force [--dry-run]
status [--json]
export --scope <id> --sort one_year --limit all|100|500
reset-running
```

配置错误返回 2，网络准备失败返回 3，数据库错误返回 4，用户停止返回 130。

- [ ] **Step 4: 运行 CLI 测试并提交**

Run: `python -m pytest tests\test_cli.py -v`

Expected: PASS。

```powershell
git add src\csindex_local\cli.py tests\test_cli.py pyproject.toml
git commit -m "feat: add command line operations"
```

---

### Task 9: Tkinter 图形界面

**Files:**
- Create: `src/csindex_local/gui.py`
- Create: `src/csindex_local/main.py`
- Create: `tests/test_gui_state.py`

**Interfaces:**
- Consumes: `Crawler`、`CrawlControl`、`ExcelExporter`
- Produces: `AppWindow(root, services)`
- Produces: `UiState.reduce(event: CrawlEvent) -> UiState`

- [ ] **Step 1: 先测试纯状态转换**

```python
def test_blocked_event_shows_cooldown():
    state = UiState.initial()
    state = state.reduce(CrawlEvent.blocked("000300", 1800))
    assert state.status_text == "官网限流冷却中"
    assert state.cooldown_seconds == 1800
    assert state.can_pause is False


def test_progress_event_updates_counts():
    state = UiState.initial().reduce(CrawlEvent.progress(total=4000, success=120, failed=2))
    assert state.progress_value == 120
    assert state.progress_max == 4000
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests\test_gui_state.py -v`

Expected: FAIL，缺少 GUI 状态模型。

- [ ] **Step 3: 实现界面和后台线程边界**

GUI 使用 `queue.Queue[CrawlEvent]` 接收后台事件，每 100ms 用 `root.after` 消费。后台线程禁止直接访问 Tk 控件。关闭窗口时设置停止信号、等待当前请求结束，并在最多 25 秒后提示用户仍在安全退出。

界面包括范围、更新模式、开始/暂停/继续/停止/导出、进度条、计数、当前指数、当前接口、冷却时间、预计剩余时间和摘要日志。

- [ ] **Step 4: 运行状态测试和手工冒烟**

Run:

```powershell
python -m pytest tests\test_gui_state.py -v
python -m csindex_local.main --demo
```

Expected: 测试 PASS；演示模式不联网，窗口可正常切换运行、暂停、冷却和完成状态。

- [ ] **Step 5: 提交 GUI**

```powershell
git add src\csindex_local\gui.py src\csindex_local\main.py tests\test_gui_state.py
git commit -m "feat: add windows crawl dashboard"
```

---

### Task 10: 模拟 HTTP 端到端集成测试

**Files:**
- Create: `tests/fake_csindex_server.py`
- Create: `tests/test_integration.py`

**Interfaces:**
- Consumes: 完整应用服务
- Produces: 可配置返回 200、403、404、超时和坏 JSON 的本地 HTTP 服务

- [ ] **Step 1: 编写 20 指数端到端测试**

```python
def test_twenty_indices_resume_and_export(fake_server, app_factory, tmp_path):
    app = app_factory(fake_server.base_url, tmp_path)
    run_id = app.prepare_codes([f"{i:06d}" for i in range(20)])
    app.run_until_successes(run_id, 13)
    app.simulate_process_restart()
    app.run_to_completion(run_id)
    path = app.export(run_id, tmp_path / "result.xlsx")
    assert app.progress(run_id).success_tasks == 40
    assert app.database.count_complete_snapshots() == 20
    assert path.exists()
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests\test_integration.py -v`

Expected: FAIL，伪服务或应用工厂尚未完成。

- [ ] **Step 3: 实现伪服务器场景**

伪服务按路径返回固定日期的数据，支持在第 N 次请求返回拦截页、单代码业务 404、一次超时和一次坏 JSON。测试配置注入零等待虚拟时钟，不允许真实等待 7 秒。

- [ ] **Step 4: 覆盖恢复与阻断场景**

补充断言：403 后没有立刻请求；普通 404 只失败一个任务；成功任务重启后不重复；日期不一致进入异常明细。

- [ ] **Step 5: 运行全量测试并提交**

Run:

```powershell
python -m pytest -v
```

Expected: 所有单元和集成测试 PASS，无真实官网请求。

```powershell
git add tests\fake_csindex_server.py tests\test_integration.py
git commit -m "test: cover resumable crawl end to end"
```

---

### Task 11: README、真实接口分级验证与 EXE 打包

**Files:**
- Create: `README.md`
- Create: `scripts/build.ps1`
- Create: `scripts/smoke_test.ps1`
- Create: `config.example.json`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: 已测试的 GUI/CLI 应用
- Produces: `dist/中证指数本地数据中心.exe`
- Produces: 可复制的发布目录和构建说明

- [ ] **Step 1: 编写真实接口只读冒烟命令**

`scripts/smoke_test.ps1` 必须按 1、20、100 三档显式接收参数，默认只跑 1 条，不能自动进入下一档：

```powershell
param([ValidateSet(1,20,100)][int]$Count = 1)
python -m csindex_local.cli crawl --scope "$Count" --mode force
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m csindex_local.cli export --scope "fixed:$Count" --sort one_year --limit all
```

- [ ] **Step 2: 运行离线发布前验证**

Run:

```powershell
python -m pytest -v
python -m csindex_local.cli init
python -m csindex_local.cli status --json
```

Expected: 测试 PASS，数据库初始化成功，状态 JSON 字段完整。

- [ ] **Step 3: 经用户确认后依次执行真实 1/20/100 条验证**

Run one level at a time:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\smoke_test.ps1 -Count 1
powershell -ExecutionPolicy Bypass -File scripts\smoke_test.ps1 -Count 20
powershell -ExecutionPolicy Bypass -File scripts\smoke_test.ps1 -Count 100
```

Expected: 每档抓取和导出均成功；如出现 WAF 阻断，停止升级档位，保留数据库断点并等待冷却。

- [ ] **Step 4: 编写 Nuitka 构建脚本**

`scripts/build.ps1` 使用当前 Python 3.12 环境创建干净构建目录，运行：

```powershell
python -m nuitka --standalone --onefile --windows-console-mode=disable `
  --enable-plugin=tk-inter `
  --include-package=openpyxl `
  --output-filename='中证指数本地数据中心.exe' `
  src\csindex_local\main.py
```

脚本不得删除项目根目录、`data` 或 `exports`；仅清理已解析并验证位于项目内的 `build` 临时目录。

- [ ] **Step 5: 构建并执行 EXE 冒烟测试**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
powershell -ExecutionPolicy Bypass -File scripts\smoke_test.ps1 -Count 1
```

Expected: EXE 启动 GUI；首次运行创建配置和数据库；1 条真实数据能够断点保存并导出。

- [ ] **Step 6: 完成 README 和最终测试**

README 必须包含：双击使用、范围含义、首次耗时、暂停续传、官网空值说明、WAF 冷却、数据位置、导出方法、CLI 排障和升级方式。

Run:

```powershell
python -m pytest -v
git status --short
```

Expected: 所有测试 PASS；只存在预期的发布产物或已提交源码。

- [ ] **Step 7: 提交交付文件**

```powershell
git add README.md scripts config.example.json .gitignore
git commit -m "build: package windows local data center"
```
