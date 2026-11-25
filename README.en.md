# SkyTiles

A local drone-photo library and Cesium globe viewer. Built with Vue 3, TypeScript, FastAPI, SQLite and Pillow; distributed as a standalone Windows executable.

See the [Chinese guide](README.md) for complete instructions.

## Download and run (Windows 10/11, 64-bit)

**[Download SkyTiles.exe](https://github.com/Z3-Lab/SkyTiles/releases/latest/download/SkyTiles.exe)** · [View the latest release](https://github.com/Z3-Lab/SkyTiles/releases/latest)

Save `SkyTiles.exe` in a writable folder and double-click it. The app opens in your default browser and runs locally. It includes everything it needs, so you can use it without installing Python or Node.js. Import photos or folders through the app, or migrate an existing `public/img-index/index.json` and its old `public` directory.

The photo index, thumbnails and settings are stored in `data/` beside the executable. Original photos stay in their existing folders. **When upgrading, replace the EXE and keep the entire data directory. Quit the service before backing it up.** Closing a browser tab does not stop the service; use the Exit button. The service defaults to port 8765 and selects an available port when necessary.

Legacy migration preserves coordinates, altitude and timestamps without reparsing EXIF. Migration indexes originals in place. Missing originals remain in the index and can be reconnected by repeating migration with the correct directory. Content hashes prevent duplicate photos.

The bundled basic globe works offline. Configure your Cesium ion token in Settings for online satellite imagery and terrain.

## Development

Requires Windows, Python 3.12+, Node.js `^20.19.0 || >=22.12.0`.

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

Licensed under [Apache 2.0](LICENSE).
