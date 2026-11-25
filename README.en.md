# SkyTiles

A local drone-photo library and Cesium globe viewer. Built with Vue 3, TypeScript, FastAPI, SQLite and Pillow; distributed as a standalone Windows executable.

See the [Chinese guide](README.md) for complete instructions.

## Windows application

Run `release/SkyTiles.exe`. It starts a loopback-only HTTP service and opens your default browser. Import photos or folders through the UI, or migrate an existing `public/img-index/index.json` and its old `public` directory.

Photos and metadata are stored in `data/` beside the executable. **When upgrading, replace the EXE and keep the entire data directory. Quit the service before backing it up.** Closing a browser tab does not stop the service; use the Exit button. The service defaults to port 8765 and selects an available port when necessary.

Legacy migration preserves coordinates, altitude and timestamps without reparsing EXIF. Originals are copied into managed storage; missing originals remain as metadata and can be repaired by repeating migration with the correct directory. Content hashes prevent duplicate photos.

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
.\.venv\Scripts\python.exe scripts/package_windows.py
```

PyInstaller bundles the frontend, Cesium assets, backend and Python runtime in `release/SkyTiles.exe`. User data and legacy public images are excluded. Build the Windows executable on Windows.

The library uses paginated lists, viewport-based map queries, grid clustering above 3000 visible locations, on-demand rendering, a bounded import worker and SQLite WAL. This release is designed for local personal use, without multi-user authentication.

Licensed under [Apache 2.0](LICENSE).
