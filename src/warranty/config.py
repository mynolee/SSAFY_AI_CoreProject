"""Private paths and opt-in runtime configuration."""
from dataclasses import dataclass
from pathlib import Path
import os

from .errors import ServiceError


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    config_dir: Path
    demo_mode: bool = True
    remote_models: bool = False
    real_uploads: bool = False
    max_settlement_attempts: int = 3

    def __post_init__(self):
        if type(self.max_settlement_attempts) is not int or not 1 <= self.max_settlement_attempts <= 10:
            raise ServiceError("CONFIG_INVALID", "정산 재시도 한도는 1~10회로 설정해 주세요.", 503, False)
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        object.__setattr__(self, "config_dir", Path(self.config_dir).resolve())

    @classmethod
    def from_env(cls):
        base = Path.home() / ".warranty"
        try:
            attempts = int(os.environ.get("MAX_SETTLEMENT_ATTEMPTS") or "3")
        except ValueError:
            raise ServiceError("CONFIG_INVALID", "정산 재시도 한도는 1~10회로 설정해 주세요.", 503, False) from None
        return cls(Path(os.environ.get("PRIVATE_DATA_DIR") or base / "data").resolve(),
                   Path(os.environ.get("PRIVATE_CONFIG_DIR") or base / "config").resolve(),
                   os.environ.get("DEMO_MODE", "true").lower() == "true",
                   os.environ.get("ENABLE_REMOTE_MODELS", "false").lower() == "true",
                   os.environ.get("ENABLE_REAL_UPLOADS", "false").lower() == "true",
                   attempts)

    def prepare(self):
        repo = Path(__file__).resolve().parents[2]
        for target in (self.data_dir, self.config_dir):
            if target == repo or repo in target.parents:
                raise ValueError("Private storage must be outside the project")
        for target in (self.data_dir, self.config_dir):
            target.mkdir(parents=True, exist_ok=True)
