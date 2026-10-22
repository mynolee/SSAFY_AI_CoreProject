"""Private paths and opt-in runtime configuration."""
from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    config_dir: Path
    demo_mode: bool = True
    remote_models: bool = False
    real_uploads: bool = False

    def __post_init__(self):
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        object.__setattr__(self, "config_dir", Path(self.config_dir).resolve())

    @classmethod
    def from_env(cls):
        base = Path.home() / ".warranty"
        return cls(Path(os.environ.get("PRIVATE_DATA_DIR") or base / "data").resolve(),
                   Path(os.environ.get("PRIVATE_CONFIG_DIR") or base / "config").resolve(),
                   os.environ.get("DEMO_MODE", "true").lower() == "true",
                   os.environ.get("ENABLE_REMOTE_MODELS", "false").lower() == "true",
                   os.environ.get("ENABLE_REAL_UPLOADS", "false").lower() == "true")

    def prepare(self):
        repo = Path(__file__).resolve().parents[2]
        for target in (self.data_dir, self.config_dir):
            if target == repo or repo in target.parents:
                raise ValueError("Private storage must be outside the project")
        for target in (self.data_dir, self.config_dir):
            target.mkdir(parents=True, exist_ok=True)
