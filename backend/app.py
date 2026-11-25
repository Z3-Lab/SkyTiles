import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .database import Database, photo_json, photo_version, read_config
from .exports import ExportService
from .dialogs import choose_directory

from .imports import ImportService
from .library import LibraryError, LibraryService
from .spatial import view_filter

logger = logging.getLogger(__name__)


class NewImport(BaseModel):
    name: str = Field(default="照片导入", min_length=1, max_length=200)


class MapConfig(BaseModel):
    cesiumToken: str = Field(default="", max_length=4096)


class ClearLibrary(BaseModel):
    confirmation: Literal["CLEAR"]
    expectedCount: int = Field(ge=0)


class PhotoReference(BaseModel):
    id: int = Field(gt=0)
    version: str = Field(min_length=1, max_length=100)


class NewExport(BaseModel):
    photos: list[PhotoReference] = Field(min_length=1, max_length=100000)
    destination: str = Field(min_length=1, max_length=32768)
    folderName: str = Field(default='', max_length=100)


class FolderSelection(BaseModel):
    initialDirectory: str = Field(default='', max_length=32768)


class LocalImport(BaseModel):
    root: str = Field(default='', max_length=32768)
    files: list[str] = Field(default_factory=list, max_length=100000)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.discover()
    settings.prepare()
    database = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app):
        database.initialize()
        app.state.imports = ImportService(settings, database)
        app.state.library = LibraryService(settings, database, app.state.imports.library_lock)
        app.state.imports.cleanup = app.state.library.collect_garbage
        app.state.exports = ExportService(settings, database, app.state.library)
        app.state.library.collect_garbage()
        app.state.imports.compact_existing()
        try:
            yield
        finally:
            app.state.exports.close()
            app.state.imports.close()

    app = FastAPI(title="SkyTiles", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs" if settings.dev else None, redoc_url=None)
    app.state.database = database
    app.state.settings = settings
    app.state.shutdown_callback = None
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.exception_handler(LibraryError)
    async def library_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    @app.middleware("http")
    async def local_writes(request: Request, call_next):
        # File-import APIs can access local directories. Reject cross-site browser requests.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            allowed = {f"http://{request.headers.get('host')}"}
            if settings.dev:
                allowed.update({"http://localhost:5173", "http://127.0.0.1:5173"})
            origin = request.headers.get("origin")
            if request.headers.get("x-skytiles-client") != "1" or (origin and origin not in allowed):
                return JSONResponse({"detail": "请通过 SkyTiles 本地界面操作"}, status_code=403)
        return await call_next(request)

    def job_or_404(job_id):
        job = database.job(job_id)
        if not job:
            raise HTTPException(404, "导入任务不存在")
        return job

    @app.get("/api/health")
    def health():
        return {"app": "SkyTiles", "version": "0.1.0",
                "library": hashlib.sha256(str(settings.data_dir).encode()).hexdigest()}

    @app.get("/api/config")
    def config():
        saved = read_config(settings.data_dir)
        return {"cesiumToken": os.environ.get("SKYTILES_CESIUM_TOKEN", saved.get("cesiumToken", "")),
                "dataDir": str(settings.data_dir), "legacyRoot": str(settings.app_root / "public"),
                "exportRoot": str(settings.app_root / "exports")}

    @app.put("/api/config")
    def save_config(value: MapConfig):
        path = settings.data_dir / "config.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return {"saved": True}

    def filters(q="", status="all"):
        clauses, parameters = ["1=1"], []
        if q:
            clauses.append("instr(lower(relative_path),lower(?))>0")
            parameters.append(q)
        if status == "gps":
            clauses.append("latitude IS NOT NULL")
        elif status == "no_gps":
            clauses.append("latitude IS NULL")
        elif status == "missing":
            clauses.append("media_path IS NULL AND source_path IS NULL")
        elif status != "all":
            raise HTTPException(422, "未知的照片筛选条件")
        return " AND ".join(clauses), parameters

    @app.get("/api/stats")
    def stats(q: str = Query("", max_length=300), status: str = "all"):
        where, params = filters(q, status)
        with database.connect() as db:
            row = db.execute(f"""SELECT COUNT(*) AS total,
                COUNT(latitude) AS withGps,
                COALESCE(SUM(media_path IS NULL AND source_path IS NULL),0) AS missing,
                MIN(longitude) AS west, MAX(longitude) AS east,
                MIN(latitude) AS south, MAX(latitude) AS north
                FROM photos WHERE {where}""", params).fetchone()
        return {**dict(row), "withoutGps": row["total"] - row["withGps"]}

    @app.get("/api/photos")
    def photos(q: str = Query("", max_length=300), status: str = "all",
               page: int = Query(1, ge=1), page_size: int = Query(40, ge=1, le=200)):
        where, params = filters(q, status)
        with database.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0]
            rows = db.execute(f"SELECT * FROM photos WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                              (*params, page_size, (page - 1) * page_size)).fetchall()
        return {"items": [photo_json(row) for row in rows], "total": total, "page": page, "pageSize": page_size}

    @app.get("/api/photos/{photo_id}")
    def photo(photo_id: int):
        with database.connect() as db:
            row = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404, "照片不存在")
        return photo_json(row)

    @app.delete("/api/photos/{photo_id}")
    def remove_photo(photo_id: int, version: str = Query(..., min_length=1, max_length=100)):
        return app.state.library.remove_photo(photo_id, version)

    @app.get("/api/library")
    def library_info():
        return app.state.library.info()

    @app.delete("/api/library")
    def clear_library(value: ClearLibrary):
        return app.state.library.clear(value.expectedCount)

    def media_file(photo_id, thumb=False):
        with database.connect() as db:
            row = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404, "原图缺失，请重新导入原图或迁移旧目录")
        try:
            path = (app.state.library.managed_path(row['thumb_path']) if thumb and row['thumb_path']
                    else app.state.library.original_path(row))
        except FileNotFoundError as error:
            raise HTTPException(404, str(error)) from error
        if not path.is_file():
            raise HTTPException(404, "图片文件已被移动或删除")
        media_type = {'.webp': 'image/webp', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png'}.get(path.suffix.lower())
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/photos/{photo_id}/image")
    def original(photo_id: int):
        return media_file(photo_id)

    @app.get("/api/photos/{photo_id}/thumbnail")
    def thumb(photo_id: int):
        return media_file(photo_id, True)

    @app.get("/api/map")
    def map_points(west: float = Query(-180, ge=-180, le=180), east: float = Query(180, ge=-180, le=180),
                   south: float = Query(-90, ge=-90, le=90), north: float = Query(90, ge=-90, le=90),
                   q: str = Query("", max_length=300), status: str = "all",
                   view: str | None = Query(None, max_length=4096)):
        if south > north:
            raise HTTPException(422, "南北边界无效")
        where, params = filters(q, status)
        longitude_clause = "longitude BETWEEN ? AND ?" if west <= east else "(longitude>=? OR longitude<=?)"
        where += ' AND latitude IS NOT NULL'
        if view is None:
            where += f" AND latitude BETWEEN ? AND ? AND {longitude_clause}"
            params += [south, north, west, east]
        with database.connect() as db:
            if view is not None:
                where += ' AND ' + view_filter(db, view)
            total = db.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0]
            if total <= 3000:
                rows = db.execute(f"SELECT id,latitude,longitude,altitude,1 AS count FROM photos WHERE {where}", params).fetchall()
            else:
                if view is not None:
                    bounds = db.execute(f'''SELECT MIN(longitude),MAX(longitude),MIN(latitude),MAX(latitude)
                        FROM photos WHERE {where}''', params).fetchone()
                    west, east, south, north = bounds
                # At most ~1600 grid clusters, with longitude unwrapped across the date line.
                lon_span = (east - west) if west <= east else (360 - west + east)
                lon_step, lat_step = max(lon_span / 40, 0.000001), max((north - south) / 40, 0.000001)
                rows = db.execute(f"""SELECT NULL AS id, AVG(latitude) AS latitude,
                    AVG(CASE WHEN longitude<? THEN longitude+360 ELSE longitude END) AS longitude,
                    AVG(COALESCE(altitude,0)) AS altitude, COUNT(*) AS count FROM photos WHERE {where}
                    GROUP BY CAST((CASE WHEN longitude<? THEN longitude+360 ELSE longitude END-?)/? AS INT),
                    CAST((latitude-?)/? AS INT)""",
                    [west, *params, west, west, lon_step, south, lat_step]).fetchall()
        points = [{**dict(row), "longitude": row["longitude"] - 360 if row["longitude"] > 180 else row["longitude"]} for row in rows]
        return {"items": points, "total": total, "clustered": total > 3000}

    @app.get("/api/map/selection")
    def selection_points(west: float = Query(-180, ge=-180, le=180), east: float = Query(180, ge=-180, le=180),
                         south: float = Query(-90, ge=-90, le=90), north: float = Query(90, ge=-90, le=90),
                         q: str = Query('', max_length=300), status: str = 'all',
                         after: int = Query(0, ge=0), snapshot: int | None = Query(None, ge=0),
                         limit: int = Query(2000, ge=1, le=5000),
                         view: str | None = Query(None, max_length=4096)):
        if south > north:
            raise HTTPException(422, '南北边界无效')
        where, params = filters(q, status)
        lon = 'longitude BETWEEN ? AND ?' if west <= east else '(longitude>=? OR longitude<=?)'
        where += ' AND latitude IS NOT NULL'
        if view is None:
            where += f' AND latitude BETWEEN ? AND ? AND {lon}'
            params += [south, north, west, east]
        where += ' AND id>? AND id<=?'
        with database.connect() as db:
            if view is not None:
                where += ' AND ' + view_filter(db, view)
            if snapshot is None:
                snapshot = db.execute('SELECT COALESCE(MAX(id),0) FROM photos').fetchone()[0]
            rows = db.execute(f'SELECT * FROM photos WHERE {where} ORDER BY id LIMIT ?',
                              [*params, after, snapshot, limit + 1]).fetchall()
        return {'items': [{'id': row['id'], 'version': photo_version(row), 'latitude': row['latitude'],
                           'longitude': row['longitude'], 'altitude': row['altitude']} for row in rows[:limit]],
                'snapshot': snapshot, 'next': rows[limit - 1]['id'] if len(rows) > limit else None}

    @app.post('/api/exports', status_code=202)
    def new_export(value: NewExport):
        return app.state.exports.start(value.photos, value.destination, value.folderName)

    @app.get('/api/exports')
    def exports():
        with database.connect() as db:
            rows = db.execute('SELECT * FROM export_jobs ORDER BY created_at DESC LIMIT 30').fetchall()
        return {'items': [dict(row) for row in rows]}

    @app.get('/api/exports/{job_id}')
    def export_status(job_id: str):
        return app.state.exports.status(job_id)

    @app.post('/api/dialogs/export-folder')
    def export_folder(value: FolderSelection | None = None):
        service = app.state.exports
        if not service.picker_lock.acquire(blocking=False):
            raise HTTPException(409, '文件夹选择窗口已经打开，请查看任务栏')
        try:
            return {'path': choose_directory(value.initialDirectory if value else '')}
        finally:
            service.picker_lock.release()

    @app.post('/api/dialogs/import-photos')
    def import_photo_dialog(value: FolderSelection | None = None):
        from .dialogs import choose_files
        service = app.state.exports
        if not service.picker_lock.acquire(blocking=False):
            raise HTTPException(409, '文件选择窗口已经打开')
        try:
            return {'paths': choose_files(value.initialDirectory if value else '') or []}
        finally:
            service.picker_lock.release()

    @app.post('/api/imports/local', status_code=202)
    def local_import(value: LocalImport):
        if bool(value.root.strip()) == bool(value.files):
            raise HTTPException(422, '请选择原图文件或一个原图文件夹')
        if value.root:
            root = Path(value.root.strip())
            if not root.is_absolute() or not root.is_dir():
                raise HTTPException(422, '请填写已有文件夹的完整路径')
            if root.resolve().is_relative_to(settings.data_dir.resolve()):
                raise HTTPException(422, '请选择 data 目录之外的原图文件夹')
            value.root = str(root.resolve())
        elif any(not Path(path).is_absolute() or '\x00' in path for path in value.files):
            raise HTTPException(422, '原图文件必须使用本机完整路径')
        job_id = app.state.imports.new_job('photos', f'建立原图索引 · {Path(value.root).name if value.root else str(len(value.files)) + " 张"}')
        app.state.imports.start(job_id, local=value.model_dump())
        return {'id': job_id}

    @app.post("/api/imports/photos", status_code=201)
    def new_import(value: NewImport):
        raise HTTPException(410, '已改为原图路径索引，请刷新界面并使用本机照片或文件夹导入')

    @app.post("/api/imports/{job_id}/files")
    @app.post("/api/imports/{job_id}/start")
    def old_photo_upload(job_id: str):
        raise HTTPException(410, '照片不再上传或复制，请刷新后使用原图路径索引导入')

    @app.post("/api/imports/legacy", status_code=202)
    def legacy_import(file: Annotated[UploadFile, File()], root: Annotated[str, Form()] = ""):
        try:
            raw = file.file.read(128 * 1024 * 1024 + 1)
            if len(raw) > 128 * 1024 * 1024:
                raise ValueError("索引文件不能超过 128 MB")
            payload = json.loads(raw.decode("utf-8-sig"))
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                raise ValueError("请选择旧版 index.json，文件中需要包含非空的 items 数组")
            legacy_root = Path(root.strip()) if root.strip() else settings.app_root / "public"
            if (legacy_root / "public" / "imgs").is_dir():
                legacy_root = legacy_root / "public"
            # Missing directories are valid: metadata-only migration can be repaired later.
            legacy_root = legacy_root.resolve()
        except (ValueError, TypeError, OSError) as error:
            raise HTTPException(422, f"无法读取旧索引：{error}") from error
        finally:
            file.file.close()
        job_id = app.state.imports.new_job("legacy", file.filename or "JSON 迁移", total=len(items))
        app.state.imports.start(job_id, {"items": items, "root": str(legacy_root)})
        return {"id": job_id}

    @app.get("/api/imports")
    def imports():
        with database.connect() as db:
            jobs = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 20").fetchall()
        return {"items": [dict(job) for job in jobs]}

    @app.get("/api/imports/{job_id}")
    def import_status(job_id: str):
        job = job_or_404(job_id)
        with database.connect() as db:
            issues = db.execute("SELECT filename,reason FROM job_issues WHERE job_id=? ORDER BY id LIMIT 100",
                                (job_id,)).fetchall()
            count = db.execute("SELECT COUNT(*) FROM job_issues WHERE job_id=?", (job_id,)).fetchone()[0]
        return {**job, "issues": [dict(issue) for issue in issues], "issueCount": count}

    @app.delete("/api/imports/{job_id}")
    def cancel_upload(job_id: str):
        job_or_404(job_id)
        with database.connect() as db:
            changed = db.execute("UPDATE jobs SET state='cancelled' WHERE id=? AND state='uploading'",
                                 (job_id,)).rowcount
            if not changed:
                raise HTTPException(409, "只能取消尚未提交的上传任务")
            rows = db.execute("SELECT path FROM staged_files WHERE job_id=?", (job_id,)).fetchall()
            db.execute("DELETE FROM staged_files WHERE job_id=?", (job_id,))
        for row in rows:
            Path(row[0]).unlink(missing_ok=True)
        staging = settings.data_dir / "staging" / job_id
        try:
            staging.rmdir()
        except OSError:
            pass
        return {"cancelled": True}

    @app.post("/api/shutdown")
    def shutdown():
        if app.state.exports.picker_lock.locked():
            raise HTTPException(409, '请先关闭文件夹选择窗口')
        with database.connect() as db:
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('uploading','queued','running')").fetchone()[0]
            exporting = db.execute("SELECT COUNT(*) FROM export_jobs WHERE state IN ('queued','running')").fetchone()[0]
        if exporting:
            raise HTTPException(409, '请先等待导出完成')
        if active:
            raise HTTPException(409, "请先等待导入完成，或取消尚未提交的上传任务")
        if not app.state.shutdown_callback:
            raise HTTPException(409, "开发服务请在终端按 Ctrl+C 退出")
        app.state.shutdown_callback()
        return {"stopped": True}

    if settings.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")

    return app
