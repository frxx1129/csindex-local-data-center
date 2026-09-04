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
