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
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def ensure_directories(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.export_dir).mkdir(parents=True, exist_ok=True)
        (Path(self.data_dir) / "logs").mkdir(parents=True, exist_ok=True)
