# SkyTiles User and Developer Guide

A local drone-photo library and Cesium globe viewer. Built with Vue 3, TypeScript, FastAPI, SQLite and Pillow; distributed for Windows, macOS, and Linux.

[简体中文](GUIDE.md) · [Project home](../README.en.md)

## Download and run

Download the package for your system from [GitHub Releases](https://github.com/Z3-Lab/SkyTiles/releases/latest).

- **Windows 10/11, 64-bit** · Save SkyTiles.exe in a writable folder and double-click it.
- **macOS** · Choose the Apple silicon (arm64) or Intel (x86_64) ZIP, then unzip and open SkyTiles.app. Both packages are built on macOS 15. They have not been notarized. If macOS blocks the app, check the source and verify the Release checksum. See [Apple’s guidance](https://support.apple.com/en-us/102445).
- **Linux x86-64** · Extract the archive and run ./SkyTiles from the extracted SkyTiles folder. The package is built on Ubuntu 22.04. It needs a desktop session and browser. The file picker uses X11 or XWayland. You can also enter a full folder path manually.

SkyTiles opens in your default browser. Choose **Import Photos** to select photos or a folder. Use **Exit** in SkyTiles to stop the local service.

### Library and upgrades

| Platform | Default data directory |
| --- | --- |
| Windows | data/ beside SkyTiles.exe |
| macOS | ~/Library/Application Support/SkyTiles |
| Linux | $XDG_DATA_HOME/skytiles or ~/.local/share/skytiles |

The photo index, thumbnails and settings are stored there. Original photos stay in their existing folders. Exit SkyTiles before backing up the full data directory or replacing the application. Keep the data directory and back up original photos separately.

Legacy migration preserves coordinates, altitude and timestamps without reparsing EXIF. Migration indexes originals in place. Missing originals remain in the index and can be reconnected by repeating migration with the correct directory. Content hashes prevent duplicate photos.

The bundled basic globe works offline. Configure your Cesium ion token in Settings for online satellite imagery and terrain.

## Development

Requires Python 3.12+ and Node.js `^20.19.0 || >=22.12.0`. The commands below are for Windows. On macOS and Linux, create the environment with python3 and replace .\.venv\Scripts\python.exe with .venv/bin/python. For Linux source runs, install Tkinter to use the picker.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock.txt
npm ci
```

Run the backend with `.\.venv\Scripts\python.exe launcher.py --dev --no-browser`, then run `npm run dev` in another terminal. Open `http://127.0.0.1:5173`.

For a production source run, execute `npm run build`, then `.\.venv\Scripts\python.exe launcher.py`.

## Build and test

```powershell
npm run build
npm run lint
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The library uses paginated lists, viewport-based map queries, grid clustering above 3000 visible locations, on-demand rendering, a bounded import worker and SQLite WAL. This release is designed for local personal use, without multi-user authentication.

Licensed under [Apache 2.0](../LICENSE).
