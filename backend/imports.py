import hashlib
import json
import logging
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .database import now
from .images import (coordinates, legacy_relative, normalize_date, number, parse_exif,
                     file_hash, relative_path, EXTENSIONS, thumbnail, under_root)

logger = logging.getLogger(__name__)


class ImportService:
    """One bounded worker keeps image decoding off the API and serializes writes."""

    def __init__(self, settings, database):
        self.settings = settings
        self.db = database
        self.library_lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photo-import")
        self.cleanup = lambda: None
        with self.db.connect() as db:
            stale = db.execute("SELECT id FROM jobs WHERE state IN ('interrupted','cancelled','failed','completed')").fetchall()
            db.execute("DELETE FROM staged_files WHERE job_id IN (SELECT id FROM jobs WHERE state='interrupted')")
        staging_root = (settings.data_dir / "staging").resolve()
        for row in stale:
            candidate = staging_root / row["id"]
            if len(row["id"]) == 32 and candidate.resolve().is_relative_to(staging_root):
                shutil.rmtree(candidate, ignore_errors=True)

    def close(self):
        self.stop.set()
        self.worker.shutdown(wait=True, cancel_futures=True)

    def new_job(self, kind, name, state="uploading", total=0):
        with self.library_lock:
            job_id = uuid.uuid4().hex
            self.db.create_job(job_id, kind, name, state, total)
            (self.settings.data_dir / "staging" / job_id).mkdir()
            return job_id

    def start(self, job_id, legacy=None, local=None, compact=False):
        with self.db.connect() as db:
            changed = db.execute("UPDATE jobs SET state='queued' WHERE id=? AND state='uploading'",
                                 (job_id,)).rowcount
        if not changed:
            raise ValueError("任务已提交或已结束")
        self.worker.submit(self.run, job_id, legacy, local, compact)

    def local_files(self, value):
        root = Path(value['root']).resolve() if value.get('root') else None
        if root:
            def scan_error(error):
                raise error
            for directory, names, files in os.walk(root, followlinks=False, onerror=scan_error):
                names[:] = [name for name in names if not (Path(directory) / name).is_junction()
                            and not (Path(directory) / name).is_symlink()
                            and (Path(directory) / name).resolve() != self.settings.data_dir.resolve()]
                if self.stop.is_set():
                    raise InterruptedError('程序退出，索引扫描已中断')
                for name in files:
                    path = Path(directory) / name
                    if path.suffix.lower() in EXTENSIONS:
                        yield path, path.relative_to(root).as_posix()
        else:
            paths = [Path(path) for path in value['files']]
            try:
                common = Path(os.path.commonpath([str(path.parent) for path in paths]))
            except ValueError:
                common = None
            for path in paths:
                yield path, path.relative_to(common).as_posix() if common else path.name

    def compact_existing(self):
        with self.db.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM photos WHERE thumb_path IS NOT NULL AND thumb_path NOT LIKE '%.v2.webp'").fetchone()[0]
        if count:
            job = self.new_job('maintenance', '压缩已有缩略图', total=count)
            self.start(job, compact=True)

    def run(self, job_id, legacy=None, local=None, compact=False):
        try:
            with self.db.connect() as db:
                db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
            if compact:
                with self.db.connect() as db:
                    records = db.execute("SELECT * FROM photos WHERE thumb_path IS NOT NULL AND thumb_path NOT LIKE '%.v2.webp'").fetchall()
            elif legacy is not None:
                records = legacy['items']
            else:
                records = list(self.local_files(local))
                with self.db.connect() as db:
                    db.execute('UPDATE jobs SET total=? WHERE id=?', (len(records), job_id))
            for record in records:
                if self.stop.is_set():
                    raise InterruptedError("程序退出，任务中断。已导入的数据保留，重新导入可自动去重。")
                label = "无效记录"
                try:
                    if compact:
                        label = record['relative_path']
                        # Thumbnails are derived files; source originals are never changed.
                        source = under_root(self.settings.data_dir, record['thumb_path'])
                        thumb = thumbnail(source, self.settings.data_dir, record['content_hash'] or str(record['id']))
                        with self.db.connect() as db:
                            db.execute('UPDATE photos SET thumb_path=?,updated_at=? WHERE id=?', (thumb, now(), record['id']))
                            db.execute('INSERT OR IGNORE INTO file_gc(path) VALUES(?)', (record['thumb_path'],))
                        result = {'outcome': 'duplicate'}
                    elif legacy is not None:
                        if not isinstance(record, dict):
                            raise ValueError("JSON 图片条目必须是对象")
                        label = str(record.get("relativePath") or record.get("filename") or record.get("imageUrl") or label)
                        result = self.import_legacy(record, Path(legacy["root"]), job_id)
                    else:
                        path, label = record
                        result = self.import_photo(path, label, job_id)
                    self.db.record_result(job_id, **result)
                except Exception as error:
                    logger.warning("Import failed for %s: %s", label, error)
                    self.db.record_result(job_id, "error", issue=(label, str(error)))
            self.db.finish_job(job_id, "completed")
        except InterruptedError as error:
            self.db.finish_job(job_id, "interrupted", str(error))
        except Exception as error:
            logger.exception("Import task failed")
            self.db.finish_job(job_id, "failed", str(error))
        finally:
            with self.db.connect() as db:
                db.execute("DELETE FROM staged_files WHERE job_id=?", (job_id,))
            staging = self.settings.data_dir / "staging" / job_id
            # This directory is always an internally generated job UUID under data/staging.
            shutil.rmtree(staging, ignore_errors=True)
            self.cleanup()

    def save(self, values, job_id, legacy_key=None, content_hash=None):
        with self.db.connect() as db:
            existing = None
            if content_hash:
                existing = db.execute("SELECT * FROM photos WHERE content_hash=?", (content_hash,)).fetchone()
            by_legacy = db.execute("SELECT * FROM photos WHERE legacy_key=?", (legacy_key,)).fetchone() if legacy_key else None
            existing = existing or by_legacy
            if existing:
                # A missing JSON record can be repaired by a later file or JSON import.
                if by_legacy and by_legacy["id"] != existing["id"]:
                    for field in ('media_path', 'thumb_path'):
                        if by_legacy[field]:
                            db.execute('INSERT OR IGNORE INTO file_gc(path) VALUES(?)', (by_legacy[field],))
                    db.execute("DELETE FROM photos WHERE id=?", (by_legacy["id"],))
                if legacy_key or values.get('source_path') or not existing["media_path"]:
                    updates = dict(values)
                    if not legacy_key and existing["legacy_key"]:
                        # Uploading the original must not overwrite trusted migrated metadata.
                        for field in ("latitude", "longitude", "altitude", "captured_at"):
                            updates[field] = existing[field]
                    updates['source_path'] = values.get('source_path') or existing['source_path']
                    updates["media_path"] = None if values.get('source_path') else existing['media_path']
                    updates["thumb_path"] = values["thumb_path"] or existing["thumb_path"]
                    for field in ('media_path', 'thumb_path'):
                        if existing[field] and existing[field] != updates[field]:
                            db.execute('INSERT OR IGNORE INTO file_gc(path) VALUES(?)', (existing[field],))
                    columns = list(updates)
                    db.execute(f"UPDATE photos SET {','.join(c + '=?' for c in columns)}, "
                               "legacy_key=COALESCE(?,legacy_key), content_hash=COALESCE(?,content_hash), updated_at=? WHERE id=?",
                               (*updates.values(), legacy_key, content_hash, now(), existing["id"]))
                return "duplicate"
            fields = {**values, "legacy_key": legacy_key, "content_hash": content_hash,
                      "batch_id": job_id, "created_at": now(), "updated_at": now()}
            db.execute(f"INSERT INTO photos({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                       tuple(fields.values()))
            return "imported"

    def import_photo(self, path, relative, job_id):
        # Check content before EXIF/thumbnail decoding so repeat imports stay cheap.
        path = self.external_source(path)
        relative = relative_path(relative)
        digest = file_hash(path)
        with self.db.connect() as db:
            existing = db.execute("SELECT * FROM photos WHERE content_hash=?", (digest,)).fetchone()
        metadata = {field: existing[field] for field in ('latitude', 'longitude', 'altitude', 'captured_at')} if existing else parse_exif(path)
        thumb = thumbnail(path, self.settings.data_dir, digest)
        values = {"filename": Path(relative).name, "relative_path": relative,
                  "media_path": None, 'source_path': str(path), "thumb_path": thumb, **metadata}
        # Match a missing migration by path; disambiguate differing coordinates when present.
        with self.db.connect() as db:
            candidates = db.execute("SELECT * FROM photos WHERE relative_path=? AND media_path IS NULL AND source_path IS NULL",
                                    (relative,)).fetchall()
        legacy_key = candidates[0]["legacy_key"] if len(candidates) == 1 else None
        if existing and existing["legacy_key"]:
            legacy_key = existing["legacy_key"]
            for field in ("latitude", "longitude", "altitude", "captured_at"):
                values[field] = existing[field]
        elif legacy_key:
            for field in ("latitude", "longitude", "altitude", "captured_at"):
                values[field] = candidates[0][field]
        outcome = self.save(values, job_id, legacy_key, digest)
        return {"outcome": outcome, "without_gps": values["latitude"] is None}

    def external_source(self, path):
        path = path.resolve()
        if path.is_relative_to(self.settings.data_dir.resolve()):
            raise ValueError('请选择 data 目录之外的原图；图库内部文件不能作为外部原图索引')
        return path

    def import_legacy(self, item, root, job_id):
        relative = legacy_relative(item)
        lat, lon = coordinates(item.get("latitude"), item.get("longitude"))
        altitude = number(item.get("altitude"), "高程")
        captured = normalize_date(item.get("capturedAt"))
        # Metadata-based key also works after the legacy directory is moved.
        identity = json.dumps([relative, lat, lon, captured], ensure_ascii=False)
        legacy_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        source = under_root(root, "imgs/" + relative)
        digest = source_path = thumb = None
        issue = None
        if source.is_file():
            source = self.external_source(source)
            digest, source_path = file_hash(source), str(source)
            # Reuse existing thumbnails, without reparsing old originals' EXIF.
            thumb_url = item.get("thumbUrl")
            old_thumb = None
            if isinstance(thumb_url, str) and thumb_url.startswith("/thumbs/"):
                from urllib.parse import unquote
                old_thumb = under_root(root, unquote(thumb_url.lstrip("/")))
            try:
                if old_thumb and old_thumb.is_file():
                    # Normalize the thumbnail rather than trusting its claimed extension.
                    thumb = thumbnail(old_thumb, self.settings.data_dir, digest)
                else:
                    thumb = thumbnail(source, self.settings.data_dir, digest)
            except Exception as error:
                issue = (relative, f"元数据与原图路径已保存，缩略图生成失败：{error}")
        else:
            issue = (relative, "原图缺失：已保留元数据。选择正确的旧版 public 目录后重新迁移可补齐。")
        values = {"filename": Path(relative).name, "relative_path": relative,
                  "latitude": lat, "longitude": lon, "altitude": altitude,
                  "captured_at": captured, "media_path": None, 'source_path': source_path, "thumb_path": thumb}
        outcome = self.save(values, job_id, legacy_key, digest)
        with self.db.connect() as db:
            saved = db.execute("SELECT media_path,source_path FROM photos WHERE legacy_key=?", (legacy_key,)).fetchone()
        missing = not saved or not (saved["media_path"] or saved['source_path'])
        if source_path is None and not missing:
            issue = None  # The old directory moved, but the managed original is already safe.
        return {"outcome": outcome, "without_gps": lat is None, "missing": missing, "issue": issue}
