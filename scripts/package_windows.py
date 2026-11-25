"""Build a self-contained Windows executable; never include the user's data."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.platform != "win32":
        raise SystemExit("请在 Windows 上构建 Windows EXE。")
    npm = shutil.which("npm.cmd")
    if not npm:
        raise SystemExit("未找到 npm，请安装 Node.js 并重新打开终端。")
    subprocess.run([npm, "run", "build"], cwd=ROOT, check=True)
    build_env = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(ROOT / "build" / "pyinstaller-cache")}
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--noconsole", "--name", "SkyTiles", "--distpath", str(ROOT / "release"),
        "--workpath", str(ROOT / "build" / "pyinstaller"), "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'dist'}{os.pathsep}web", "--collect-submodules", "uvicorn",
        str(ROOT / "launcher.py"),
    ], cwd=ROOT, check=True, env=build_env)
    shutil.copyfile(ROOT / "README.md", ROOT / "release" / "使用说明.md")
    shutil.copyfile(ROOT / "LICENSE", ROOT / "release" / "LICENSE")
    print(f"EXE: {ROOT / 'release' / 'SkyTiles.exe'}")


if __name__ == "__main__":
    main()
