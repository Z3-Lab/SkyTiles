"""Library removal, with durable retries for files temporarily locked on Windows."""
import logging
from pathlib import Path, PurePosixPath, PureWindowsPath

from .database import photo_version

logger = logging.getLogger(__name__)
ACTIVE = "('uploading','queued','running')"


class LibraryError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class LibraryService:
    ROOTS = {"media", "thumbs", "staging"}

    def __init__(self, settings, database, lock):
        self.root = settings.data_dir.resolve()
        self.db = database
        self.lock = lock

    def info(self):
        with self.db.connect() as db:
            return {
                "photoCount": db.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
                "batchCount": db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "activeImports": db.execute(f"SELECT COUNT(*) FROM jobs WHERE state IN {ACTIVE}").fetchone()[0],
                "activeExports": db.execute("SELECT COUNT(*) FROM export_jobs WHERE state IN ('queued','running')").fetchone()[0],
                "cleanupPending": db.execute("SELECT COUNT(*) FROM file_gc").fetchone()[0],
                "linkedPhotos": db.execute('SELECT COUNT(*) FROM photos WHERE source_path IS NOT NULL').fetchone()[0],
                "managedPhotos": db.execute('SELECT COUNT(*) FROM photos WHERE media_path IS NOT NULL').fetchone()[0],
            }

    def managed_path(self, relative):
        value = str(relative).replace("\\", "/")
        path = PurePosixPath(value)
        if (path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts
                or len(path.parts) < 2 or path.parts[0] not in self.ROOTS):
            raise LibraryError("文件路径不属于影像库的托管目录，已停止清理")
        candidate = self.root / path
        # Validate every component before a file operation, including Windows junctions.
        for component in (candidate, *candidate.parents):
            if component == self.root:
                break
            if component.is_symlink() or component.is_junction():
                raise LibraryError("托管目录包含链接或目录联接，已停止清理")
        approved = (self.root / path.parts[0]).resolve()
        if not approved.is_relative_to(self.root) or not candidate.resolve().is_relative_to(approved):
            raise LibraryError("文件路径超出影像库目录，已停止清理")
        return candidate

    def original_path(self, row):
        if row['source_path']:
            source = Path(row['source_path'])
            if not source.is_absolute():
                raise LibraryError('原图索引路径无效', 422)
            if source.is_file():
                return source
        if row['media_path']:
            return self.managed_path(row['media_path'])
        raise FileNotFoundError('原图已移动、删除或磁盘未连接，请重新关联原图目录')

    @staticmethod
    def require_idle(db):
        if db.execute("SELECT 1 FROM export_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
            raise LibraryError("正在导出影像，请等待复制完成后再移除或清空")
        if db.execute(f"SELECT 1 FROM jobs WHERE state IN {ACTIVE} LIMIT 1").fetchone():
            raise LibraryError("正在建立索引或压缩缩略图，请在导入记录中等待任务完成后重试")

    def remove_photo(self, photo_id, version):
        with self.lock:
            with self.db.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.require_idle(db)
                row = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
                if not row:
                    raise LibraryError("该影像已被移除，请刷新影像库", 404)
                if photo_version(row) != version:
                    raise LibraryError("影像信息已变化，请刷新影像库后重新确认移除")
                paths = [row[name] for name in ("media_path", "thumb_path") if row[name]]
                for path in paths:
                    self.managed_path(path)
                db.executemany("INSERT OR IGNORE INTO file_gc(path) VALUES(?)", [(path,) for path in paths])
                db.execute("DELETE FROM photos WHERE id=?", (photo_id,))
            pending = self.collect_garbage()
            return {"removed": 1, "cleanupPending": pending}

    def clear(self, expected_count):
        with self.lock:
            with self.db.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.require_idle(db)
                total = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
                if total != expected_count:
                    raise LibraryError("影像库数量已变化，请关闭确认框后重新确认清空")
                paths = []
                for name in sorted(self.ROOTS):
                    directory = self.root / name
                    # Inspect the directory itself, even if it is an empty link.
                    self.managed_path(f"{name}/.path-check")
                    if not directory.exists():
                        continue
                    for path in directory.rglob("*"):
                        relative = path.relative_to(self.root).as_posix()
                        self.managed_path(relative)
                        if path.is_file():
                            paths.append((relative,))
                db.executemany("INSERT OR IGNORE INTO file_gc(path) VALUES(?)", paths)
                db.execute("DELETE FROM photos")
                db.execute("DELETE FROM staged_files")
                db.execute("DELETE FROM job_issues")
                db.execute("DELETE FROM jobs")
                db.execute("DELETE FROM export_issues")
                db.execute("DELETE FROM export_jobs")
            pending = self.collect_garbage()
            return {"removed": total, "cleanupPending": pending}

    def collect_garbage(self):
        # Deletions commit before unlinking. A crash/locked file leaves a durable
        # retry record, rather than rolling back rows whose files were already removed.
        with self.lock:
            with self.db.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if (db.execute(f"SELECT 1 FROM jobs WHERE state IN {ACTIVE} LIMIT 1").fetchone()
                        or db.execute("SELECT 1 FROM export_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone()):
                    return db.execute("SELECT COUNT(*) FROM file_gc").fetchone()[0]
                queued = db.execute("SELECT path FROM file_gc").fetchall()
                for row in queued:
                    relative = row["path"]
                    if db.execute("SELECT 1 FROM photos WHERE media_path=? OR thumb_path=? LIMIT 1",
                                  (relative, relative)).fetchone():
                        db.execute("DELETE FROM file_gc WHERE path=?", (relative,))
                        continue
                    try:
                        path = self.managed_path(relative)
                        path.unlink(missing_ok=True)
                    except (OSError, LibraryError) as error:
                        logger.warning("File cleanup deferred for %s: %s", relative, error)
                        continue
                    db.execute("DELETE FROM file_gc WHERE path=?", (relative,))
                    parent = path.parent
                    while parent != self.root / path.relative_to(self.root).parts[0]:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
                return db.execute("SELECT COUNT(*) FROM file_gc").fetchone()[0]
