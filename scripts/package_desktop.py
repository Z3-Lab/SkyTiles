"""Build the distributable for the current desktop OS without including library data."""
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    system = sys.platform
    if system not in ("win32", "darwin", "linux"):
        raise SystemExit(f"Unsupported packaging platform: {system}")
    npm = shutil.which("npm.cmd" if system == "win32" else "npm")
    if not npm:
        raise SystemExit("未找到 npm，请安装 Node.js。")
    subprocess.run([npm, "run", "build"], cwd=ROOT, check=True)

    release = ROOT / "release"
    work = ROOT / "build" / "desktop"
    work.mkdir(parents=True, exist_ok=True)
    output = release if system == "win32" else work / "dist"
    options = {
        "win32": ("--onefile", "--noconsole", "--exclude-module", "tkinter"),
        "darwin": ("--onedir", "--windowed", "--exclude-module", "tkinter",
                   "--osx-bundle-identifier", "io.github.z3lab.skytiles"),
        "linux": ("--onefile",),
    }[system]
    build_env = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(work / "cache")}
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", *options,
        "--name", "SkyTiles", "--distpath", str(output), "--workpath", str(work / "pyinstaller"),
        "--specpath", str(work), "--add-data", f"{ROOT / 'dist'}{os.pathsep}web",
        "--collect-submodules", "uvicorn", str(ROOT / "launcher.py"),
    ], cwd=ROOT, check=True, env=build_env)

    release.mkdir(exist_ok=True)
    guide = ROOT / "docs" / "GUIDE.md"
    license_path = ROOT / "LICENSE"
    if system == "win32":
        artifact = release / "SkyTiles.exe"
        shutil.copyfile(guide, release / "使用说明.md")
        shutil.copyfile(license_path, release / "LICENSE")
    elif system == "darwin":
        artifact = release / f"SkyTiles-macos-{platform.machine()}.zip"
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(output / "SkyTiles.app"), str(artifact)], check=True)
    else:
        artifact = release / f"SkyTiles-linux-{platform.machine()}.tar.gz"
        with tarfile.open(artifact, "w:gz") as archive:
            archive.add(output / "SkyTiles", "SkyTiles/SkyTiles")
            archive.add(guide, "SkyTiles/使用说明.md")
            archive.add(license_path, "SkyTiles/LICENSE")
    print(f"SkyTiles: {artifact}")


if __name__ == "__main__":
    main()
