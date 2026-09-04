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


def test_rejects_max_delay_below_min_delay(tmp_path: Path):
    config = AppConfig.default(tmp_path)
    config.request_delay_max_seconds = 6.9
    with pytest.raises(ValueError, match="最大请求间隔不能小于最小请求间隔"):
        config.validate()


@pytest.mark.parametrize("batch_size", [0, 26])
def test_rejects_batch_size_outside_safe_range(tmp_path: Path, batch_size: int):
    config = AppConfig.default(tmp_path)
    config.batch_size = batch_size
    with pytest.raises(ValueError, match="批次大小必须在 1 到 25 之间"):
        config.validate()


def test_rejects_short_batch_rest(tmp_path: Path):
    config = AppConfig.default(tmp_path)
    config.batch_rest_seconds = 149
    with pytest.raises(ValueError, match="批次休息不能低于 150 秒"):
        config.validate()


def test_save_and_load_round_trip(tmp_path: Path):
    path = tmp_path / "config.json"
    expected = AppConfig.default(tmp_path)
    expected.save(path)
    assert AppConfig.load(path) == expected


def test_ensure_directories_creates_runtime_directories(tmp_path: Path):
    config = AppConfig.default(tmp_path)
    config.ensure_directories()
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "exports").is_dir()
    assert (tmp_path / "data" / "logs").is_dir()
