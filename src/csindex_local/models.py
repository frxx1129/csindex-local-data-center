from dataclasses import dataclass
from enum import Enum
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
class MetricRow:
    """The latest exportable metrics for one index inside a frozen scope."""

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


@dataclass(frozen=True)
class CrawlScope:
    id: str
    name: str
    scope_type: str
    scope_value: str
    codes: tuple[str, ...]


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


@dataclass(frozen=True)
class ScopeSelection:
    kind: str
    value: int | tuple[str, ...]
    regenerate: bool = False
    # CLI code-file scopes use the file-content digest as their stable ID.
    # Keeping this optional preserves the existing service API for callers that
    # let the crawler derive the ID from the selected codes.
    scope_id: str | None = None


class UpdateMode(str, Enum):
    MISSING = "missing"
    UPDATE = "update"
    FORCE = "force"


@dataclass(frozen=True)
class CrawlEvent:
    """Immutable worker event safe to hand to CLI and GUI consumers."""

    kind: str
    run_id: str
    progress: RunProgress
    index_code: str | None = None
    endpoint: str | None = None
    message: str | None = None
    available_at: str | None = None
