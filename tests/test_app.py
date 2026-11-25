import io
import json
import shutil
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image, TiffImagePlugin

from backend.app import create_app
from backend.config import Settings
from backend.database import Database, now
from backend.images import normalize_date
from backend.library import LibraryError

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"X-SkyTiles-Client": "1"}


def image_bytes(color="red", gps=False):
    image = Image.new("RGB", (80, 50), color)
    output = io.BytesIO()
    exif = Image.Exif()
    if gps:
        rational = TiffImagePlugin.IFDRational
        exif[34853] = {1: "N", 2: (rational(31), rational(12), rational(0)),
                       3: "E", 4: (rational(121), rational(30), rational(0)),
                       5: b"\x00", 6: rational(123)}
        exif[34665] = {36867: "2026:10:30 08:09:10", 36881: "+08:00"}
    image.save(output, "JPEG", exif=exif)
    return output.getvalue()


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".test-output").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".test-output")
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root, self.root / "data", self.root / "web", True)
        self.app = create_app(self.settings)
        self.client_context = TestClient(self.app, headers=HEADERS)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def wait(self, job_id):
        for _ in range(200):
            result = self.client.get(f"/api/imports/{job_id}")
            self.assertEqual(result.status_code, 200, result.text)
            job = result.json()
            if job["state"] not in ("queued", "running"):
                self.assertEqual(job["state"], "completed", job)
                return job
            time.sleep(0.02)
        self.fail("Import did not finish")

    def upload(self, entries):
        paths = []
        for name, content in entries:
            path = self.root / 'sources' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            paths.append(str(path))
        response = self.client.post('/api/imports/local', json={'files': paths})
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait(response.json()['id'])

    def migrate(self, items, root=None):
        response = self.client.post("/api/imports/legacy", files={"file":
            ("index.json", json.dumps({"version": 1, "items": items}, ensure_ascii=False).encode("utf-8-sig"), "application/json")},
            data={"root": str(root or self.root / "public")})
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait(response.json()["id"])

    def test_upload_gps_no_gps_invalid_and_deduplication(self):
        gps = image_bytes(gps=True)
        plain = image_bytes("blue")
        job = self.upload([("flight/a.jpg", gps), ("b.jpg", plain), ("bad.jpg", b"broken")])
        self.assertEqual((job["imported"], job["without_gps"], job["errors"]), (2, 1, 1))
        summary = self.client.get("/api/stats").json()
        self.assertEqual((summary["total"], summary["withGps"], summary["withoutGps"]), (2, 1, 1))
        self.assertEqual(self.upload([("renamed.jpg", gps)])["duplicates"], 1)
        rows = self.client.get("/api/photos?status=gps").json()["items"]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["latitude"], 31.2)
        self.assertEqual(rows[0]["altitude"], 123)
        self.assertEqual(rows[0]["capturedAt"], "2026-10-30T08:09:10+08:00")
        self.assertEqual(self.client.get(rows[0]["imageUrl"]).content, gps)
        self.assertEqual(self.client.get(rows[0]["thumbUrl"]).status_code, 200)

    def test_legacy_both_formats_no_exif_and_missing_repair(self):
        source = self.root / "public" / "imgs" / "批次"
        source.mkdir(parents=True)
        (source / "a.jpg").write_bytes(image_bytes())
        items = [
            {"id": 12, "relativePath": "批次/a.jpg", "filename": "a.jpg", "latitude": 30,
             "longitude": 120, "altitude": 50, "capturedAt": "2020-01-01T12:00:00Z"},
            {"id": 13, "imageUrl": "/imgs/b.jpg", "latitude": 31, "longitude": 121, "capturedAt": None},
        ]
        with patch("backend.imports.parse_exif", side_effect=AssertionError("Must not parse legacy EXIF")):
            first = self.migrate(items)
            self.assertEqual((first["imported"], first["missing"]), (2, 1))
            again = self.migrate(items)
            self.assertEqual((again["imported"], again["duplicates"]), (0, 2))
            (source.parent / "b.jpg").write_bytes(image_bytes("green"))
            repaired = self.migrate(items, self.root)  # Accept project root as well as public.
            self.assertEqual((repaired["duplicates"], repaired["missing"]), (2, 0))
        summary = self.client.get("/api/stats").json()
        self.assertEqual((summary["total"], summary["missing"]), (2, 0))
        photo = self.client.get("/api/photos?q=批次").json()["items"][0]
        self.assertEqual(photo["altitude"], 50)
        self.assertEqual(photo["capturedAt"], "2020-01-01T12:00:00+00:00")
        self.assertEqual(self.client.get(photo["imageUrl"]).content, image_bytes())
        self.assertEqual(self.migrate(items, self.root / "moved-old-directory")["missing"], 0)

    def test_reindex_updates_source_and_restores_thumbnail(self):
        original = image_bytes(gps=True)
        self.upload([("a.jpg", original)])
        with self.app.state.database.connect() as db:
            row = db.execute("SELECT * FROM photos").fetchone()
        Path(row['source_path']).unlink()
        (self.settings.data_dir / row["thumb_path"]).unlink()
        job = self.upload([("renamed.jpeg", original)])
        self.assertEqual(job["duplicates"], 1)
        photo = self.client.get("/api/photos").json()["items"][0]
        self.assertEqual(self.client.get(photo["imageUrl"]).content, original)
        self.assertEqual(self.client.get(photo["thumbUrl"]).status_code, 200)

    def test_missing_legacy_repaired_by_upload_preserves_metadata(self):
        self.migrate([{"relativePath": "a.jpg", "latitude": 22, "longitude": 113,
                       "altitude": 400, "capturedAt": "2000-01-01T12:00:00Z"}])
        job = self.upload([("a.jpg", image_bytes(gps=True))])
        self.assertEqual(job["duplicates"], 1)
        photos = self.client.get("/api/photos").json()["items"]
        self.assertEqual(len(photos), 1)
        self.assertEqual((photos[0]["latitude"], photos[0]["longitude"], photos[0]["altitude"]), (22, 113, 400))
        self.assertFalse(photos[0]["missing"])

    def test_migration_validation_is_per_item(self):
        items = [
            {"relativePath": "../outside.jpg", "latitude": 30, "longitude": 120},
            {"relativePath": "x.jpg", "latitude": 999, "longitude": 120},
            {"imageUrl": "/imgs/%2e%2e/outside.jpg", "latitude": 30, "longitude": 120},
            {"relativePath": "ok.jpg", "latitude": 0, "longitude": 0},
            "not an object",
        ]
        result = self.migrate(items)
        self.assertEqual((result["processed"], result["imported"], result["errors"]), (5, 1, 4))
        self.assertEqual(self.client.get("/api/stats").json()["withGps"], 1)

    def test_pagination_filters_map_grid_and_dateline(self):
        database = self.app.state.database
        with database.connect() as db:
            db.executemany("""INSERT INTO photos(filename,relative_path,latitude,longitude,created_at,updated_at)
                VALUES(?,?,?,?,?,?)""", [(f"p{i}.jpg", f"group/p{i}.jpg", 30 + (i % 60) * .001,
                120 + (i // 60) * .001, now(), now()) for i in range(3100)])
            db.executemany("""INSERT INTO photos(filename,relative_path,latitude,longitude,created_at,updated_at)
                VALUES(?,?,?,?,?,?)""", [("date.jpg", "date.jpg", 0, lon, now(), now()) for lon in (179, -179)])
        page = self.client.get("/api/photos?page=2&page_size=20&q=group").json()
        self.assertEqual((page["total"], len(page["items"])), (3100, 20))
        overview = self.client.get("/api/map?west=119&east=121&south=29&north=31").json()
        self.assertTrue(overview["clustered"])
        self.assertEqual(sum(point["count"] for point in overview["items"]), 3100)
        self.assertLess(len(overview["items"]), 2000)
        detailed = self.client.get("/api/map?west=120&east=120.002&south=30&north=30.002").json()
        self.assertFalse(detailed["clustered"])
        self.assertGreater(detailed["total"], 0)
        date = self.client.get("/api/map?west=170&east=-170&south=-10&north=10").json()
        self.assertEqual(date["total"], 2)
        self.assertEqual(self.client.get("/api/map?south=40&north=20").status_code, 422)
        selection, after, snapshot = [], 0, None
        while after is not None:
            params = dict(west=119, east=121, south=29, north=31, after=after, limit=1000, q='group')
            if snapshot is not None:
                params['snapshot'] = snapshot
            result = self.client.get('/api/map/selection', params=params).json()
            snapshot, after = result['snapshot'], result['next']
            selection.extend(result['items'])
        self.assertEqual(len(selection), 3100)  # Clustered map must not truncate selection.
        self.assertEqual(len({point['id'] for point in selection}), 3100)
        self.assertTrue(all(point['version'] for point in selection))
        date = self.client.get('/api/map/selection?west=170&east=-170&south=-10&north=10').json()
        self.assertEqual(len(date['items']), 2)
        self.assertEqual(self.client.get('/api/map/selection?status=no_gps').json()['items'], [])
        self.assertEqual(self.client.get('/api/map/selection?south=40&north=20').status_code, 422)

    def test_local_write_guard_and_upload_traversal(self):
        self.assertEqual(self.client.post("/api/imports/photos", json={"name": "x"},
            headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/imports/photos", json={"name": "x"},
            headers={"X-SkyTiles-Client": ""}).status_code, 403)
        self.assertEqual(self.client.post('/api/imports/local', json={'files': ['../outside.jpg']}).status_code, 422)
        self.assertEqual(self.client.post('/api/imports/local', json={'root': str(self.settings.data_dir)}).status_code, 422)
        self.assertEqual(self.client.post('/api/imports/photos', json={}).status_code, 410)
        self.assertEqual(self.client.post('/api/imports/old/files').status_code, 410)

    def test_configuration_persists_and_pending_jobs_recover(self):
        self.assertEqual(self.client.put("/api/config", json={"cesiumToken": "example-token"}).status_code, 200)
        self.assertEqual(self.client.get("/api/config").json()["cesiumToken"], "example-token")
        pending = self.app.state.imports.new_job('photos', 'pending')
        with TestClient(create_app(self.settings), headers=HEADERS) as restarted:
            self.assertEqual(restarted.get(f"/api/imports/{pending}").json()["state"], "interrupted")
            self.assertEqual(restarted.get("/api/config").json()["cesiumToken"], "example-token")

    def test_exif_time_does_not_invent_timezone(self):
        self.assertEqual(normalize_date("2026:10:30 12:34:56"), "2026-10-30T12:34:56")
        self.assertEqual(normalize_date("invalid"), None)

    def remove(self, photo):
        return self.client.delete(f"/api/photos/{photo['id']}", params={"version": photo["version"]})

    def clear_library(self, expected=None):
        count = self.client.get("/api/library").json()["photoCount"] if expected is None else expected
        return self.client.request("DELETE", "/api/library", json={"confirmation": "CLEAR", "expectedCount": count})

    def test_remove_photo_cleans_media_and_refreshes_queries(self):
        self.upload([("a.jpg", image_bytes(gps=True)), ("b.jpg", image_bytes("blue"))])
        photo = self.client.get("/api/photos?status=gps").json()["items"][0]
        with self.app.state.database.connect() as db:
            row = db.execute("SELECT * FROM photos WHERE id=?", (photo["id"],)).fetchone()
        response = self.remove(photo)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed": 1, "cleanupPending": 0})
        self.assertIsNone(row['media_path'])
        self.assertTrue(Path(row['source_path']).is_file())
        self.assertFalse((self.settings.data_dir / row["thumb_path"]).exists())
        self.assertEqual(self.client.get(f"/api/photos/{photo['id']}").status_code, 404)
        self.assertEqual(self.client.get(photo["imageUrl"]).status_code, 404)
        self.assertEqual(self.client.get("/api/stats").json()["total"], 1)
        self.assertEqual(self.client.get("/api/map").json()["total"], 0)
        self.assertEqual(self.remove(photo).status_code, 404)

    def test_clear_library_removes_all_data_but_preserves_settings_and_sources(self):
        original = self.root / "public/imgs/a.jpg"
        original.parent.mkdir(parents=True)
        original.write_bytes(image_bytes(gps=True))
        self.migrate([{"relativePath": "a.jpg", "latitude": 30, "longitude": 120},
                      {"relativePath": "missing.jpg", "latitude": None, "longitude": None}])
        self.client.put("/api/config", json={"cesiumToken": "keep-this-setting"})
        orphan = self.settings.data_dir / "staging/abandoned/file.bin"
        orphan.parent.mkdir()
        orphan.write_bytes(b"temporary upload")
        (self.settings.data_dir / "media/orphan.jpg").write_bytes(b"unindexed original")
        response = self.clear_library()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed": 2, "cleanupPending": 0})
        self.assertEqual(self.client.get("/api/stats").json()["total"], 0)
        self.assertEqual(self.client.get("/api/imports").json()["items"], [])
        self.assertEqual(self.client.get("/api/config").json()["cesiumToken"], "keep-this-setting")
        self.assertTrue(original.exists())
        for name in ("media", "thumbs", "staging"):
            self.assertFalse(any(path.is_file() for path in (self.settings.data_dir / name).rglob("*")))
        with self.app.state.database.connect() as db:
            for table in ("photos", "jobs", "job_issues", "staged_files", "file_gc"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(self.clear_library().status_code, 200)
        self.assertEqual(self.upload([("new.jpg", image_bytes("green"))])["imported"], 1)

    def test_removal_rejects_active_imports_wrong_confirmation_and_stale_targets(self):
        self.upload([("a.jpg", image_bytes())])
        photo = self.client.get("/api/photos").json()["items"][0]
        self.assertEqual(self.client.delete(f"/api/photos/{photo['id']}", params={"version": "stale"}).status_code, 409)
        self.assertEqual(self.client.request("DELETE", "/api/library", json={"confirmation": "wrong", "expectedCount": 1}).status_code, 422)
        self.assertEqual(self.clear_library(expected=0).status_code, 409)
        job_id = self.app.state.imports.new_job('photos', 'pending')
        self.assertEqual(self.remove(photo).status_code, 409)
        self.assertEqual(self.clear_library().status_code, 409)
        self.assertEqual(self.client.get("/api/library").json()["photoCount"], 1)
        self.client.delete(f"/api/imports/{job_id}")
        self.assertEqual(self.remove(photo).status_code, 200)

    def test_removal_never_deletes_outside_managed_directories(self):
        outside = self.root / "source.jpg"
        outside.write_bytes(image_bytes())
        self.migrate([{"relativePath": "missing.jpg", "latitude": None, "longitude": None}])
        with self.app.state.database.connect() as db:
            db.execute("UPDATE photos SET media_path='../source.jpg'")
        photo = self.client.get("/api/photos").json()["items"][0]
        self.assertEqual(self.remove(photo).status_code, 409)
        self.assertTrue(outside.exists())
        self.assertEqual(self.client.get("/api/library").json()["photoCount"], 1)
        self.assertEqual(self.clear_library().status_code, 200)
        self.assertTrue(outside.exists())

    def test_removal_keeps_files_still_referenced_by_another_photo(self):
        self.upload([("a.jpg", image_bytes())])
        first = self.client.get("/api/photos").json()["items"][0]
        with self.app.state.database.connect() as db:
            db.execute("""INSERT INTO photos(filename,relative_path,media_path,source_path,thumb_path,created_at,updated_at)
                SELECT 'alias.jpg','alias.jpg',media_path,source_path,thumb_path,created_at,updated_at FROM photos""")
        self.assertEqual(self.remove(first).json()["cleanupPending"], 0)
        alias = self.client.get("/api/photos").json()["items"][0]
        self.assertEqual(self.client.get(alias["imageUrl"]).status_code, 200)
        self.assertEqual(self.client.get(alias["thumbUrl"]).status_code, 200)
        self.assertEqual(self.remove(alias).json()["cleanupPending"], 0)
        self.assertFalse(any((self.settings.data_dir / "media").iterdir()))

    def test_locked_file_cleanup_is_durable_and_retried_on_startup(self):
        self.upload([("a.jpg", image_bytes())])
        photo = self.client.get("/api/photos").json()["items"][0]
        original_unlink = Path.unlink

        def locked_unlink(path, *args, **kwargs):
            if path.parent == self.settings.data_dir / "thumbs":
                raise PermissionError("file in use")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", locked_unlink):
            response = self.remove(photo)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cleanupPending"], 1)
        self.assertEqual(self.client.get("/api/stats").json()["total"], 0)
        with TestClient(create_app(self.settings), headers=HEADERS) as restarted:
            self.assertEqual(restarted.get("/api/library").json()["cleanupPending"], 0)
        self.assertFalse(any((self.settings.data_dir / "thumbs").iterdir()))

    def test_reimport_after_removal_has_a_new_media_cache_version(self):
        self.upload([("a.jpg", image_bytes())])
        old = self.client.get("/api/photos").json()["items"][0]
        self.assertEqual(self.remove(old).status_code, 200)
        self.upload([("b.jpg", image_bytes("blue"))])
        new = self.client.get("/api/photos").json()["items"][0]
        self.assertNotEqual(old["version"], new["version"])
        self.assertNotEqual(old["imageUrl"], new["imageUrl"])
        self.assertEqual(self.remove(old).status_code, 409)

    def start_export(self, photos=None, destination=None, folder='测试导出'):
        if photos is None:
            photos = self.client.get('/api/photos').json()['items']
        return self.client.post('/api/exports', json={
            'photos': [{'id': p['id'], 'version': p['version']} for p in photos],
            'destination': str(destination or self.root / '导出'), 'folderName': folder})

    def wait_export(self, response):
        self.assertEqual(response.status_code, 202, response.text)
        for _ in range(200):
            job = self.client.get('/api/exports/' + response.json()['id']).json()
            if job['state'] not in ('queued', 'running'):
                return job
            time.sleep(.02)
        self.fail('Export did not finish')

    def test_export_selected_originals_preserves_subfolders_bytes_and_library(self):
        original = image_bytes(gps=True)
        self.upload([('航线/航拍.jpg', original), ('b.jpg', image_bytes('blue'))])
        photo = self.client.get('/api/photos?status=gps').json()['items'][0]
        job = self.wait_export(self.start_export([photo, photo]))
        self.assertEqual((job['state'], job['total'], job['copied'], job['errors']), ('completed', 1, 1, 0))
        target = Path(job['target_dir'])
        self.assertEqual((target / '航线/航拍.jpg').read_bytes(), original)
        self.assertFalse((target / 'b.jpg').exists())
        self.assertEqual(self.client.get(photo['imageUrl']).content, original)
        self.assertEqual(self.client.get('/api/stats').json()['total'], 2)
        report = json.loads((target / '导出报告.json').read_text(encoding='utf-8'))
        self.assertEqual(report['items'][0]['file'], '航线/航拍.jpg')
        again = self.wait_export(self.start_export([photo]))
        self.assertNotEqual(again['target_dir'], str(target))
        self.assertEqual((target / '航线/航拍.jpg').read_bytes(), original)
        self.assertEqual(len(self.client.get('/api/exports').json()['items']), 2)

    def test_export_missing_originals_and_case_insensitive_filename_collisions(self):
        self.upload([('first/same.jpg', image_bytes()), ('second/SAME.jpg', image_bytes('blue')), ('c.jpg', image_bytes('green'))])
        with self.app.state.database.connect() as db:
            db.execute('UPDATE photos SET relative_path=filename')
        self.migrate([{'relativePath': 'missing.jpg', 'latitude': 30, 'longitude': 120}])
        with self.app.state.database.connect() as db:
            row = db.execute("SELECT * FROM photos WHERE filename='c.jpg'").fetchone()
        Path(row['source_path']).unlink()
        job = self.wait_export(self.start_export())
        self.assertEqual((job['copied'], job['missing'], job['errors'], len(job['issues'])), (2, 2, 0, 2))
        images = list(Path(job['target_dir']).glob('*.jpg'))
        self.assertEqual(len(images), 2)
        self.assertEqual(len({image.name.casefold() for image in images}), 2)
        self.assertEqual({image.read_bytes() for image in images}, {image_bytes(), image_bytes('blue')})

    def test_export_rejects_stale_selection_and_invalid_destination_before_copying(self):
        self.upload([('a.jpg', image_bytes())])
        photo = self.client.get('/api/photos').json()['items'][0]
        self.assertEqual(self.start_export([{**photo, 'version': 'stale'}]).status_code, 409)
        self.assertFalse((self.root / '导出').exists())
        self.assertEqual(self.start_export([photo, {**photo, 'version': 'stale'}]).status_code, 409)
        self.assertEqual(self.start_export([{**photo, 'id': 9999}]).status_code, 409)
        self.assertEqual(self.start_export(destination='relative/path').status_code, 422)
        self.assertEqual(self.start_export(destination=self.settings.data_dir / 'media/exports').status_code, 422)
        self.assertEqual(self.start_export(destination='C:\\bad\x00path').status_code, 422)
        self.assertEqual(self.client.post('/api/exports', json={'photos': [], 'destination': str(self.root)}).status_code, 422)
        self.assertEqual(self.client.get('/api/exports').json()['items'], [])

    def test_export_refuses_unmanaged_source_and_sanitizes_output_names(self):
        self.upload([('a.jpg', image_bytes())])
        with self.app.state.database.connect() as db:
            db.execute("UPDATE photos SET relative_path='../CON.jpg'")
        job = self.wait_export(self.start_export(folder='../危险:*'))
        target = Path(job['target_dir'])
        self.assertEqual(target.parent, self.root / '导出')
        self.assertEqual((target / 'a.jpg').read_bytes(), image_bytes())
        with self.app.state.database.connect() as db:
            db.execute("UPDATE photos SET media_path='../source.jpg'")
        self.assertEqual(self.start_export().status_code, 409)

    def test_active_export_blocks_removal_clear_and_shutdown_then_preserves_exported_files(self):
        self.upload([('a.jpg', image_bytes())])
        photo = self.client.get('/api/photos').json()['items'][0]
        started, release = threading.Event(), threading.Event()
        service = self.app.state.exports
        original = service.copy_original

        def slow_copy(source, target):
            started.set()
            self.assertTrue(release.wait(5))
            original(source, target)

        try:
            with patch.object(service, 'copy_original', slow_copy):
                response = self.start_export()
                self.assertTrue(started.wait(5))
                self.assertEqual(self.client.get('/api/library').json()['activeExports'], 1)
                self.assertEqual(self.remove(photo).status_code, 409)
                self.assertEqual(self.clear_library().status_code, 409)
                self.assertEqual(self.client.post('/api/shutdown').status_code, 409)
                release.set()
                job = self.wait_export(response)
        finally:
            release.set()
        self.assertEqual(self.clear_library().status_code, 200)
        self.assertEqual((Path(job['target_dir']) / 'a.jpg').read_bytes(), image_bytes())
        self.assertEqual(self.client.get('/api/exports').json()['items'], [])

    def test_export_copy_failure_reports_reason(self):
        self.upload([('a.jpg', image_bytes())])
        with patch('backend.exports.ExportService.copy_original', side_effect=PermissionError('目标不可写')):
            job = self.wait_export(self.start_export())
        self.assertEqual((job['state'], job['copied'], job['errors']), ('completed', 0, 1))
        self.assertIn('目标不可写', job['issues'][0]['reason'])
        self.assertFalse(list(Path(job['target_dir']).glob('*.jpg')))

    def test_interrupted_copy_removes_only_its_partial_file(self):
        service = self.app.state.exports
        source, target = self.root / 'original.jpg', self.root / 'partial.jpg'
        source.write_bytes(image_bytes())
        service.stopping.set()
        try:
            with self.assertRaises(InterruptedError):
                service.copy_original(source, target)
            self.assertFalse(target.exists())
            self.assertEqual(source.read_bytes(), image_bytes())
        finally:
            service.stopping.clear()
        target.write_bytes(b'pre-existing')
        with self.assertRaises(FileExistsError):
            service.copy_original(source, target)
        self.assertEqual(target.read_bytes(), b'pre-existing')

    def test_export_pending_recovery_and_native_picker_cancel(self):
        with self.app.state.database.connect() as db:
            db.execute("INSERT INTO export_jobs(id,state,target_dir,total,created_at) VALUES('pending','running',?,2,?)",
                       (str(self.root / 'old-export'), now()))
        with TestClient(create_app(self.settings), headers=HEADERS) as restarted:
            job = restarted.get('/api/exports/pending').json()
            self.assertEqual(job['state'], 'interrupted')
            self.assertIn('保留', job['message'])
        with patch('backend.app.choose_directory', return_value=None):
            self.assertEqual(self.client.post('/api/dialogs/export-folder').json(), {'path': None})
        with patch('backend.app.choose_directory', return_value=str(self.root)):
            self.assertEqual(self.client.post('/api/dialogs/export-folder').json()['path'], str(self.root))

    def test_folder_dialog_passes_initial_path_and_recovers_after_error(self):
        with patch('backend.app.choose_directory', side_effect=LibraryError('系统窗口不可用', 422)):
            self.assertEqual(self.client.post('/api/dialogs/export-folder').status_code, 422)
        self.assertFalse(self.app.state.exports.picker_lock.locked())
        with patch('backend.app.choose_directory', return_value=str(self.root)) as picker:
            response = self.client.post('/api/dialogs/export-folder', json={'initialDirectory': str(self.root / '中文目录')})
            self.assertEqual(response.json()['path'], str(self.root))
            picker.assert_called_once_with(str(self.root / '中文目录'))
        with self.app.state.exports.picker_lock:
            self.assertEqual(self.client.post('/api/dialogs/export-folder').status_code, 409)
            self.assertEqual(self.client.post('/api/shutdown').status_code, 409)
        with patch('backend.app.choose_directory', return_value=None):
            self.assertEqual(self.client.post('/api/dialogs/export-folder').json(), {'path': None})

    def test_low_camera_angles_keep_elevated_points_and_selection_consistent(self):
        node = shutil.which('node') or str(Path('C:/Program Files/nodejs/node.exe'))
        if not Path(node).is_file():
            self.skipTest('Node.js is required to generate independent Cesium reference geometry')
        reference = json.loads(subprocess.run([node, str(ROOT / 'tests/map_view_reference.mjs')],
            cwd=ROOT, check=True, capture_output=True, text=True).stdout)
        with self.app.state.database.connect() as db:
            db.executemany('''INSERT INTO photos(id,filename,relative_path,latitude,longitude,altitude,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)''', [(p['id'], f"{p['id']}.jpg", f"flight/{p['id']}.jpg",
                p['latitude'], p['longitude'], p['altitude'], now(), now()) for p in reference['photos']])
        for case in reference['cases']:
            params = {'view': json.dumps(case['view']), 'q': 'flight', 'status': 'gps'}
            response = self.client.get('/api/map', params=params)
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(sorted(p['id'] for p in result['items']), case['expected'], case['pitch'])
            self.assertEqual(result['total'], len(case['expected']))
            selected, after = [], 0
            while after is not None:
                result = self.client.get('/api/map/selection', params={**params, 'limit': 2, 'after': after}).json()
                selected.extend(p['id'] for p in result['items'])
                after = result['next']
            self.assertEqual(selected, case['expected'], case['pitch'])
        low = next(case for case in reference['cases'] if case['pitch'] == -20)
        self.assertIn(1, low['missed'])
        self.assertIn(1, low['expected'])
        self.assertEqual(self.client.get('/api/map', params={'view': json.dumps(low['view']), 'q': 'absent'}).json()['total'], 0)
        with self.app.state.database.connect() as db:
            db.executemany('''INSERT INTO photos(filename,relative_path,latitude,longitude,altitude,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)''', [(f'{i}.jpg', f'cluster/{i}.jpg', 35.006, 120, 1700, now(), now())
                for i in range(3101)])
        clustered = self.client.get('/api/map', params={'view': json.dumps(low['view']), 'q': 'cluster'}).json()
        self.assertTrue(clustered['clustered'])
        self.assertEqual(clustered['total'], 3101)
        self.assertEqual(sum(point['count'] for point in clustered['items']), 3101)
        self.assertTrue(all(point['altitude'] == 1700 for point in clustered['items']))
        selected = self.client.get('/api/map/selection', params={
            'view': json.dumps(low['view']), 'q': 'cluster', 'limit': 5000}).json()
        self.assertEqual(len(selected['items']), 3101)

    def test_3d_map_view_rejects_malformed_and_nonfinite_geometry(self):
        invalid = ['{}', 'bad-json', json.dumps({'camera': [0, 0, 0], 'planes': [[0, 0, 0, 1]] * 6}),
                   json.dumps({'camera': [float('nan'), 0, 0], 'planes': [[1, 0, 0, 1]] * 6})]
        for value in invalid:
            for route in ('/api/map', '/api/map/selection'):
                self.assertEqual(self.client.get(route, params={'view': value}).status_code, 422)

    def test_index_only_directory_import_preserves_sources_and_bounds_thumbnail_size(self):
        source = self.root / '中文航测目录/子目录'
        source.mkdir(parents=True)
        image = Image.effect_noise((2400, 1600), 100).convert('RGB')
        image.save(source / 'noise.jpg', quality=95)
        (source / 'gps.jpg').write_bytes(image_bytes(gps=True))
        (source / 'notes.txt').write_text('ignore')
        before = {p.name: p.read_bytes() for p in source.iterdir()}
        response = self.client.post('/api/imports/local', json={'root': str(source.parent)})
        job = self.wait(response.json()['id'])
        self.assertEqual((job['total'], job['imported']), (2, 2))
        self.assertFalse(list((self.settings.data_dir / 'media').iterdir()))
        self.assertFalse(list((self.settings.data_dir / 'staging').iterdir()))
        for photo in self.client.get('/api/photos').json()['items']:
            self.assertEqual(Path(photo['sourcePath']).parent, source)
            self.assertTrue(photo['relativePath'].startswith('子目录/'))
            self.assertEqual(self.client.get(photo['imageUrl']).content, before[photo['filename']])
            response = self.client.get(photo['thumbUrl'])
            self.assertEqual(response.headers['content-type'], 'image/webp')
            self.assertLessEqual(len(response.content), 4096)
            with Image.open(io.BytesIO(response.content)) as thumb:
                self.assertLessEqual(max(thumb.size), 128)
                self.assertEqual(len(thumb.getexif()), 0)
        self.assertEqual(self.clear_library().status_code, 200)
        self.assertEqual({p.name: p.read_bytes() for p in source.iterdir()}, before)

    def test_relinking_a_managed_photo_releases_copy_after_matching_content(self):
        self.upload([('photo.jpg', image_bytes(gps=True))])
        with self.app.state.database.connect() as db:
            row = db.execute('SELECT * FROM photos').fetchone()
            managed = self.settings.data_dir / 'media/old-copy.jpg'
            managed.write_bytes(Path(row['source_path']).read_bytes())
            db.execute('UPDATE photos SET source_path=NULL,media_path=?', ('media/old-copy.jpg',))
            db.execute('DROP INDEX photos_source')
            db.execute('ALTER TABLE photos DROP COLUMN source_path')
        self.app.state.database.initialize()  # Upgrade an actual v3 photos schema without source_path.
        old = self.client.get('/api/photos').json()['items'][0]
        self.assertEqual(self.client.get(old['imageUrl']).status_code, 200)
        job = self.upload([('relocated/renamed.jpg', image_bytes(gps=True))])
        self.assertEqual(job['duplicates'], 1)
        self.app.state.library.collect_garbage()
        current = self.client.get('/api/photos').json()['items'][0]
        self.assertEqual(current['id'], old['id'])
        self.assertEqual(Path(current['sourcePath']).name, 'renamed.jpg')
        self.assertFalse(managed.exists())
        self.assertEqual(self.client.get(current['imageUrl']).content, image_bytes(gps=True))
        self.assertTrue((self.root / 'sources/photo.jpg').is_file())

    def test_old_library_thumbnails_compress_without_requiring_originals(self):
        self.migrate([{'relativePath': 'missing.jpg', 'latitude': 30, 'longitude': 120}])
        old_thumb = self.settings.data_dir / 'thumbs/old.jpg'
        Image.new('RGB', (400, 300), 'red').save(old_thumb, quality=90)
        with self.app.state.database.connect() as db:
            db.execute("UPDATE photos SET thumb_path='thumbs/old.jpg'")
        self.app.state.imports.compact_existing()
        for job in self.client.get('/api/imports').json()['items']:
            if job['kind'] == 'maintenance':
                self.wait(job['id'])
        self.app.state.library.collect_garbage()
        photo = self.client.get('/api/photos').json()['items'][0]
        self.assertTrue(photo['missing'])
        self.assertIsNone(photo['imageUrl'])
        self.assertEqual(self.client.get(photo['thumbUrl']).headers['content-type'], 'image/webp')
        self.assertFalse(old_thumb.exists())

    def test_json_accepts_over_32mb_and_rejects_over_128mb(self):
        content = json.dumps({'items': [{'relativePath': 'large.jpg', 'latitude': 30, 'longitude': 120}]}).encode()
        response = self.client.post('/api/imports/legacy',
            files={'file': ('large.json', content + b' ' * (33 * 1024 * 1024), 'application/json')})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.wait(response.json()['id'])['imported'], 1)
        response = self.client.post('/api/imports/legacy',
            files={'file': ('too-large.json', b' ' * (128 * 1024 * 1024 + 1), 'application/json')})
        self.assertEqual(response.status_code, 422)
        self.assertIn('128 MB', response.text)


if __name__ == "__main__":
    unittest.main()
