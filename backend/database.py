import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                    state TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0, imported INTEGER NOT NULL DEFAULT 0,
                    duplicates INTEGER NOT NULL DEFAULT 0, without_gps INTEGER NOT NULL DEFAULT 0,
                    missing INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                    message TEXT, created_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS staged_files (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    path TEXT NOT NULL, relative_path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS photos (
                    id INTEGER PRIMARY KEY, legacy_key TEXT UNIQUE, content_hash TEXT UNIQUE,
                    filename TEXT NOT NULL, relative_path TEXT NOT NULL,
                    media_path TEXT, thumb_path TEXT, latitude REAL, longitude REAL,
                    altitude REAL, captured_at TEXT, batch_id TEXT REFERENCES jobs(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    CHECK ((latitude IS NULL) = (longitude IS NULL)),
                    CHECK (latitude BETWEEN -90 AND 90),
                    CHECK (longitude BETWEEN -180 AND 180)
                );
                CREATE INDEX IF NOT EXISTS photos_location ON photos(longitude, latitude);
                CREATE INDEX IF NOT EXISTS photos_time ON photos(captured_at);
                CREATE INDEX IF NOT EXISTS photos_missing_relative ON photos(relative_path) WHERE media_path IS NULL;
                CREATE INDEX IF NOT EXISTS staged_job ON staged_files(job_id);
                CREATE TABLE IF NOT EXISTS job_issues (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    filename TEXT NOT NULL, reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS issues_job ON job_issues(job_id);
                CREATE TABLE IF NOT EXISTS file_gc (path TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS export_jobs (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, target_dir TEXT NOT NULL,
                    total INTEGER NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
                    copied INTEGER NOT NULL DEFAULT 0, missing INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0, message TEXT,
                    created_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS export_issues (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES export_jobs(id),
                    filename TEXT NOT NULL, reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS export_issues_job ON export_issues(job_id);
            """)
            if 'source_path' not in {row['name'] for row in db.execute('PRAGMA table_info(photos)')}:
                db.execute('ALTER TABLE photos ADD COLUMN source_path TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS photos_source ON photos(source_path)')
            db.execute('PRAGMA user_version=4')
            db.execute("""UPDATE jobs SET state='interrupted', message=?, finished_at=?
                WHERE state IN ('uploading','queued','running')""",
                ("上次运行时任务中断；已导入的数据保留，可以重新导入并自动去重。", now()))
            db.execute("""UPDATE export_jobs SET state='interrupted', message=?, finished_at=?
                WHERE state IN ('queued','running')""",
                ("上次导出中断；已复制的文件保留，请重新选择影像导出到新文件夹。", now()))

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def create_job(self, job_id, kind, name, state="uploading", total=0):
        with self.connect() as db:
            db.execute("INSERT INTO jobs(id,kind,name,state,total,created_at) VALUES(?,?,?,?,?,?)",
                       (job_id, kind, name, state, total, now()))

    def finish_job(self, job_id, state, message=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?, message=?, finished_at=? WHERE id=?",
                       (state, message, now(), job_id))

    def record_result(self, job_id, outcome, without_gps=False, missing=False, issue=None):
        with self.connect() as db:
            db.execute("""UPDATE jobs SET processed=processed+1,
                imported=imported+?, duplicates=duplicates+?, errors=errors+?,
                without_gps=without_gps+?, missing=missing+? WHERE id=?""",
                (int(outcome == "imported"), int(outcome == "duplicate"), int(outcome == "error"),
                 int(without_gps), int(missing), job_id))
            if issue:
                db.execute("INSERT INTO job_issues(job_id,filename,reason) VALUES(?,?,?)",
                           (job_id, *issue))


def photo_version(row):
    identity = f"{row['id']}:{row['created_at']}:{row['updated_at']}:{row['content_hash']}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def photo_json(row):
    photo_id = row["id"]
    version = photo_version(row)
    return {
        "id": photo_id, "filename": row["filename"], "relativePath": row["relative_path"],
        "latitude": row["latitude"], "longitude": row["longitude"],
        "altitude": row["altitude"], "capturedAt": row["captured_at"],
        "version": version,
        "imageUrl": f"/api/photos/{photo_id}/image?v={version}" if row["media_path"] or row['source_path'] else None,
        "thumbUrl": f"/api/photos/{photo_id}/thumbnail?v={version}" if row["thumb_path"] else None,
        "sourcePath": row['source_path'],
        "missing": not bool(row["media_path"] or row['source_path']), "batchId": row["batch_id"],
    }


def read_config(data_dir):
    path = data_dir / "config.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}
