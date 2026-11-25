import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_root: Path
    data_dir: Path
    web_dir: Path
    dev: bool = False

    @classmethod
    def discover(cls, data_dir: str | None = None, dev: bool = False):
        frozen = getattr(sys, "frozen", False)
        root = Path(sys.executable).parent if frozen else Path(__file__).resolve().parent.parent
        assets = Path(getattr(sys, "_MEIPASS", root))
        data = Path(data_dir or os.environ.get("SKYTILES_DATA_DIR", root / "data")).resolve()
        return cls(root, data, assets / ("web" if frozen else "dist"), dev)

    def prepare(self):
        for name in ("media", "thumbs", "staging", "logs"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self):
        return self.data_dir / "skytiles.sqlite3"
