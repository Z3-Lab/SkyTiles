import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_VERSION = "0.2.0"


def default_data_dir(root: Path, frozen: bool) -> Path:
    if not frozen or sys.platform == "win32":
        return root / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SkyTiles"
    xdg = Path(os.environ.get("XDG_DATA_HOME", ""))
    return (xdg if xdg.is_absolute() else Path.home() / ".local" / "share") / "skytiles"


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
        configured = data_dir or os.environ.get("SKYTILES_DATA_DIR")
        data = Path(configured).expanduser() if configured else default_data_dir(root, frozen)
        data = data.resolve()
        return cls(root, data, assets / ("web" if frozen else "dist"), dev)

    def prepare(self):
        for name in ("media", "thumbs", "staging", "logs"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)

    @property
    def export_dir(self):
        if getattr(sys, "frozen", False) and sys.platform != "win32":
            return Path.home() / "SkyTiles Exports"
        return self.app_root / "exports"

    @property
    def db_path(self):
        return self.data_dir / "skytiles.sqlite3"
