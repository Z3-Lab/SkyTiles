"""Copy selected originals into a new directory, without moving or overwriting files."""
import json
import logging
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path, PurePosixPath

from .database import now, photo_version
from .library import LibraryError

logger = logging.getLogger(__name__)


def safe_component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).rstrip(' .')[:100].rstrip(' .')
    if not value or value in ('.', '..'):
        value = '_'
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
        value = '_' + value
    return value


class ExportService:
    def __init__(self, settings, database, library):
        self.settings, self.db, self.library = settings, database, library
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='export')
        self.stopping = threading.Event()
        self.picker_lock = threading.Lock()

    def close(self):
        self.stopping.set()
        self.executor.shutdown(wait=True)

    def start(self, selections, destination, folder_name=''):
        versions = {}
        for item in selections:
            if item.id in versions and versions[item.id] != item.version:
                raise LibraryError('所选影像版本冲突，请重新框选')
            versions[item.id] = item.version
        with self.library.lock:
            with self.db.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                rows = []
                ids = list(versions)
                for start in range(0, len(ids), 500):
                    batch = ids[start:start + 500]
                    rows.extend(dict(row) for row in db.execute(
                        f"SELECT * FROM photos WHERE id IN ({','.join('?' for _ in batch)})", batch))
                if len(rows) != len(versions) or any(photo_version(row) != versions[row['id']] for row in rows):
                    raise LibraryError('所选影像已发生变化，请刷新地图后重新框选')
                # Validate all source paths before creating any output.
                for row in rows:
                    if row['media_path']:
                        if not row['media_path'].replace('\\', '/').startswith('media/'):
                            raise LibraryError('原图路径不属于影像库')
                        self.library.managed_path(row['media_path'])
                if '\x00' in destination:
                    raise LibraryError('目标文件夹路径无效', 422)
                parent = Path(destination.strip()).expanduser()
                if not parent.is_absolute():
                    raise LibraryError('请选择或填写完整的目标文件夹路径', 422)
                parent = parent.resolve()
                if parent.is_relative_to(self.settings.data_dir.resolve()):
                    raise LibraryError('请选择影像库 data 目录以外的位置', 422)
                name = safe_component(folder_name.strip() or datetime.now().strftime('SkyTiles_%Y%m%d_%H%M%S'))
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                    # mkdir is exclusive: existing exports are never overwritten.
                    for suffix in range(10000):
                        target = parent / (name if not suffix else f'{name}_{suffix + 1}')
                        try:
                            target.mkdir()
                            break
                        except FileExistsError:
                            continue
                    else:
                        raise OSError('同名文件夹过多，请修改文件夹名称')
                    job_id = uuid.uuid4().hex
                    db.execute('INSERT INTO export_jobs(id,state,target_dir,total,created_at) VALUES(?,?,?,?,?)',
                               (job_id, 'queued', str(target), len(rows), now()))
                except OSError as error:
                    raise LibraryError(f'无法创建导出目录：{error}', 422) from error
            self.executor.submit(self.run, job_id, rows, target)
        return {'id': job_id}

    def status(self, job_id):
        with self.db.connect() as db:
            row = db.execute('SELECT * FROM export_jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise LibraryError('导出记录不存在', 404)
            issues = db.execute('SELECT filename,reason FROM export_issues WHERE job_id=? ORDER BY id LIMIT 100', (job_id,)).fetchall()
        return {**dict(row), 'issues': [dict(issue) for issue in issues]}

    def copy_original(self, source, target):
        # Exclusive creation also protects against filename collisions and external writers.
        with source.open('rb') as original:
            with target.open('xb') as output:
                try:
                    while chunk := original.read(1024 * 1024):
                        if self.stopping.is_set():
                            raise InterruptedError('服务已关闭')
                        output.write(chunk)
                except BaseException:
                    output.close()
                    target.unlink(missing_ok=True)
                    raise
        try:
            shutil.copystat(source, target)
        except OSError:
            pass  # Original bytes were copied; timestamps are optional.

    def run(self, job_id, rows, target):
        report = []
        state, note = 'completed', '复制完成，原图和影像库保持不变。'
        # Reserve the report's name, case-insensitively as on Windows.
        used = {'导出报告.json'}
        try:
            with self.db.connect() as db:
                db.execute("UPDATE export_jobs SET state='running' WHERE id=?", (job_id,))
            for row in rows:
                if self.stopping.is_set():
                    raise InterruptedError('导出中断；已复制的文件保留，请重新导出到新文件夹。')
                outcome, reason, exported = 'copied', None, None
                try:
                    source = self.library.original_path(row)
                    if not source.is_file():
                        raise FileNotFoundError('原图文件已被移动或删除')
                    parts = PurePosixPath(row['relative_path'].replace('\\', '/')).parts
                    # Flatten invalid absolute/traversal entries rather than following them.
                    if not parts or '..' in parts or PurePosixPath(row['relative_path']).is_absolute():
                        parts = (row['filename'],)
                    relative = Path(*(safe_component(part) for part in parts))
                    candidate = relative
                    index = 0
                    while candidate.as_posix().casefold() in used or (target / candidate).exists():
                        index += 1
                        candidate = relative.with_name(f'{relative.stem}_{row["id"]}_{index}{relative.suffix}')
                    used.add(candidate.as_posix().casefold())
                    output = target / candidate
                    output.parent.mkdir(parents=True, exist_ok=True)
                    # Refuse links introduced into this fresh output tree.
                    if not output.resolve().is_relative_to(target.resolve()) or any(
                        path.is_symlink() or path.is_junction() for path in (output, *output.parents) if path != target.parent
                    ):
                        raise OSError('目标目录中包含链接或目录联接')
                    self.copy_original(source, output)
                    exported = candidate.as_posix()
                except InterruptedError:
                    raise
                except FileNotFoundError as error:
                    outcome, reason = 'missing', str(error)
                except (OSError, LibraryError, ValueError) as error:
                    outcome, reason = 'error', str(error)
                report.append({'id': row['id'], 'source': row['relative_path'], 'file': exported, 'status': outcome, 'reason': reason})
                with self.db.connect() as db:
                    db.execute('''UPDATE export_jobs SET processed=processed+1,copied=copied+?,missing=missing+?,errors=errors+? WHERE id=?''',
                               (int(outcome == 'copied'), int(outcome == 'missing'), int(outcome == 'error'), job_id))
                    if reason:
                        db.execute('INSERT INTO export_issues(job_id,filename,reason) VALUES(?,?,?)', (job_id, row['relative_path'], reason))
        except InterruptedError as error:
            state, note = 'interrupted', str(error)
        except Exception as error:
            logger.exception('Export failed: %s', job_id)
            state, note = 'failed', f'导出失败：{error}。已复制的文件保留。'
        finally:
            try:
                with (target / '导出报告.json').open('x', encoding='utf-8') as output:
                    json.dump({'state': state, 'items': report}, output, ensure_ascii=False, indent=2)
            except OSError:
                note += ' 导出报告未能写入。'
            with self.db.connect() as db:
                db.execute('UPDATE export_jobs SET state=?,message=?,finished_at=? WHERE id=?', (state, note, now(), job_id))
            self.library.collect_garbage()
