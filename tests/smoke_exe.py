"""Exercise the distributed desktop package from a temporary Chinese path."""
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from test_app import image_bytes
from backend.spatial import cartesian

HEADERS = {"X-SkyTiles-Client": "1"}


def wait_job(client, job_id):
    for _ in range(300):
        job = client.get(f"/api/imports/{job_id}").json()
        if job["state"] not in ("queued", "running"):
            assert job["state"] == "completed", job
            return job
        time.sleep(0.1)
    raise AssertionError("Import timed out")


def main():
    (ROOT / ".test-output").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="EXE中文验证-", dir=ROOT / ".test-output") as temporary:
        folder = Path(temporary).resolve()
        assert folder.is_relative_to((ROOT / ".test-output").resolve())
        release = ROOT / "release"
        env = dict(os.environ)
        for name in ("PYTHONHOME", "PYTHONPATH", "SKYTILES_DATA_DIR", "SKYTILES_CESIUM_TOKEN"):
            env.pop(name, None)
        flags = 0
        if sys.platform == "win32":
            executable = folder / "SkyTiles.exe"
            shutil.copyfile(release / "SkyTiles.exe", executable)
            data = folder / "data"
            system_root = env.get("SystemRoot", env.get("SYSTEMROOT", r"C:Windows"))
            env["PATH"] = os.pathsep.join([str(Path(system_root) / "System32"), system_root])
            flags = subprocess.CREATE_NO_WINDOW
        elif sys.platform == "darwin":
            artifact = release / f"SkyTiles-macos-{platform.machine()}.zip"
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(artifact), str(folder)], check=True)
            executable = folder / "SkyTiles.app/Contents/MacOS/SkyTiles"
            env.update(HOME=str(folder), PATH="")
            data = folder / "Library/Application Support/SkyTiles"
        else:
            artifact = release / f"SkyTiles-linux-{platform.machine()}.tar.gz"
            with tarfile.open(artifact) as archive:
                archive.extractall(folder, filter="data")
            executable = folder / "SkyTiles/SkyTiles"
            env.update(XDG_DATA_HOME=str(folder / "xdg"), PATH="")
            data = folder / "xdg/skytiles"
        version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        old = folder / "旧版影像" / "public"
        (old / "imgs" / "批次一").mkdir(parents=True)
        original = image_bytes(gps=True)
        (old / "imgs" / "批次一" / "航拍.jpg").write_bytes(original)
        items = [
            {"id": 10, "relativePath": "批次一/航拍.jpg", "latitude": 31.2, "longitude": 121.5,
             "altitude": 200, "capturedAt": "2020-01-01T12:00:00Z"},
            {"id": 11, "imageUrl": "/imgs/批次一/缺失.jpg", "latitude": 31.21, "longitude": 121.51,
             "altitude": 210, "capturedAt": None},
        ]
        index = json.dumps({"version": 1, "items": items}, ensure_ascii=False).encode("utf-8")
        process = None
        client = None

        def start():
            nonlocal process, client
            process = subprocess.Popen([str(executable), "--no-browser", "--port", "0"],
                cwd=ROOT, env=env, creationflags=flags)
            info = data / "service.json"
            for _ in range(600):
                if process.poll() is not None:
                    log = data / "logs" / "skytiles.log"
                    raise AssertionError(f"EXE exited {process.returncode}: {log.read_text(encoding='utf-8') if log.exists() else 'no log'}")
                try:
                    port = json.loads(info.read_text(encoding="utf-8"))["port"]
                    candidate = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=HEADERS, timeout=10)
                    if candidate.get("/api/health").status_code == 200:
                        client = candidate
                        return
                    candidate.close()
                except (OSError, ValueError, httpx.HTTPError):
                    pass
                time.sleep(0.1)
            raise AssertionError("EXE did not start within 60 seconds")

        def stop():
            if client:
                response = client.post("/api/shutdown")
                assert response.status_code == 200, response.text
                client.close()
            process.wait(timeout=30)
            assert process.returncode == 0, process.returncode

        try:
            start()
            assert client.get("/").status_code == 200
            assert "SkyTiles" in client.get("/").text
            assert client.get("/cesium/Cesium.js").status_code == 200
            assert Path(client.get("/api/config").json()["dataDir"]) == data
            assert client.get("/api/health").json()["version"] == version
            source = folder / '本机原图'
            (source / '批次一').mkdir(parents=True)
            (source / '批次一/航拍.jpg').write_bytes(original)
            (source / '无坐标.jpg').write_bytes(image_bytes('blue'))
            response = client.post('/api/imports/local', json={'root': str(source)})
            assert response.status_code == 202, response.text
            assert wait_job(client, response.json()['id'])['imported'] == 2
            assert not any((data / 'media').iterdir())
            assert not any((data / 'staging').iterdir())

            def migrate():
                response = client.post("/api/imports/legacy", files={"file": ("index.json", index, "application/json")},
                                       data={"root": str(old)})
                assert response.status_code == 202, response.text
                return wait_job(client, response.json()["id"])

            first = migrate()
            assert (first["imported"], first["duplicates"], first["missing"]) == (1, 1, 1), first
            assert migrate()["duplicates"] == 2
            (old / "imgs" / "批次一" / "缺失.jpg").write_bytes(image_bytes("green"))
            assert migrate()["missing"] == 0
            summary = client.get("/api/stats").json()
            assert (summary["total"], summary["withGps"], summary["withoutGps"], summary["missing"]) == (3, 2, 1, 0), summary
            photo = client.get("/api/photos?q=航拍").json()["items"][0]
            assert photo["altitude"] == 200
            assert hashlib.sha256(client.get(photo["imageUrl"]).content).digest() == hashlib.sha256(original).digest()
            assert client.get(photo["thumbUrl"]).status_code == 200
            assert len(client.get(photo['thumbUrl']).content) <= 4096
            assert client.get(photo['thumbUrl']).headers['content-type'] == 'image/webp'
            assert Path(photo['sourcePath']) == old / 'imgs/批次一/航拍.jpg'
            assert not any((data / 'media').iterdir())
            assert client.get("/api/map").json()["total"] == 2
            x, y, z = cartesian(photo['latitude'], photo['longitude'], photo['altitude'])
            camera = cartesian(photo['latitude'], photo['longitude'], photo['altitude'] + 500)
            view = json.dumps({'camera': camera, 'planes': [
                [1, 0, 0, 100 - x], [-1, 0, 0, 100 + x],
                [0, 1, 0, 100 - y], [0, -1, 0, 100 + y],
                [0, 0, 1, 100 - z], [0, 0, -1, 100 + z]]})
            visible = client.get('/api/map', params={'view': view}).json()
            assert [p['id'] for p in visible['items']] == [photo['id']], visible
            visible_selection = client.get('/api/map/selection', params={'view': view}).json()
            assert [p['id'] for p in visible_selection['items']] == [photo['id']], visible_selection
            selection = client.get('/api/map/selection').json()['items']
            assert len(selection) == 2 and all(point['version'] for point in selection)
            response = client.post('/api/exports', json={'photos': selection,
                'destination': str(folder / '批量导出'), 'folderName': '所选照片'})
            assert response.status_code == 202, response.text
            export_id = response.json()['id']
            for _ in range(300):
                exported = client.get('/api/exports/' + export_id).json()
                if exported['state'] not in ('queued', 'running'):
                    break
                time.sleep(.1)
            assert (exported['state'], exported['copied'], exported['errors']) == ('completed', 2, 0), exported
            exported_folder = Path(exported['target_dir'])
            assert (exported_folder / '批次一/航拍.jpg').read_bytes() == original
            assert (exported_folder / '批次一/缺失.jpg').read_bytes() == image_bytes('green')
            assert (exported_folder / '导出报告.json').is_file()
            assert client.put("/api/config", json={"cesiumToken": "test-persistence"}).status_code == 200
            stop()

            start()
            assert client.get("/api/stats").json()["total"] == 3
            assert client.get("/api/config").json()["cesiumToken"] == "test-persistence"
            assert client.get('/api/exports/' + export_id).json()['copied'] == 2
            second = subprocess.run([str(executable), "--no-browser"], cwd=ROOT, env=env,
                                    creationflags=flags, timeout=30)
            assert second.returncode == 0, (second.returncode,
                (data / "logs" / "skytiles.log").read_text(encoding="utf-8"))
            assert client.get("/api/health").json()["app"] == "SkyTiles"
            photo = client.get("/api/photos?q=航拍").json()["items"][0]
            response = client.delete(f"/api/photos/{photo['id']}", params={"version": photo["version"]})
            assert response.status_code == 200 and response.json()["cleanupPending"] == 0, response.text
            assert client.get(photo["imageUrl"]).status_code == 404
            assert client.get("/api/stats").json()["total"] == 2
            response = client.request("DELETE", "/api/library", json={"confirmation": "CLEAR", "expectedCount": 2})
            assert response.status_code == 200 and response.json()["removed"] == 2, response.text
            assert client.get("/api/map").json()["total"] == 0
            assert client.get("/api/imports").json()["items"] == []
            assert client.get('/api/exports').json()['items'] == []
            assert (exported_folder / '批次一/航拍.jpg').read_bytes() == original
            assert client.get("/api/config").json()["cesiumToken"] == "test-persistence"
            assert (old / "imgs" / "批次一" / "航拍.jpg").is_file()
            for name in ("media", "thumbs", "staging"):
                assert not any(path.is_file() for path in (data / name).rglob("*"))
            stop()
            start()
            assert client.get("/api/stats").json()["total"] == 0
            assert client.get("/api/config").json()["cesiumToken"] == "test-persistence"
            stop()
            print(json.dumps({"result": "PASS", "platform": sys.platform,
                "checks": ["standalone runtime", "Chinese paths", "platform data directory independent of cwd",
                    "bundled frontend and Cesium", "index-only local import and EXIF", "legacy index-only migration", "repeat deduplication",
                    "missing-file repair", "media and thumbnails", "restart persistence", "single instance",
                    "single photo removal", "clear library preserving settings and sources",
                    "empty library persists after restart", "graceful shutdown",
                    "selection candidates", "batch export byte-identical originals and report",
                    "export history persists across restart", "exported files survive library clear",
                    "3D elevated map visibility and matching selection", "no original copies or staged uploads",
                    "compact WebP thumbnails", "original sources survive index removal"]}))
        finally:
            if process and process.poll() is None:
                if client:
                    try:
                        client.post("/api/shutdown")
                    except httpx.HTTPError:
                        pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if sys.platform == "win32":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       creationflags=flags, capture_output=True)
                    else:
                        process.kill()
                        process.wait(timeout=10)
            if client:
                client.close()


if __name__ == "__main__":
    main()
