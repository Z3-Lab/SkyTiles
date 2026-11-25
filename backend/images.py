import hashlib
import math
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote

from PIL import ExifTags, Image, ImageOps

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("图片相对路径为空或无效")
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts:
        raise ValueError("图片路径必须位于选择的目录内")
    if path.suffix.lower() not in EXTENSIONS:
        raise ValueError("仅支持 JPG、JPEG、PNG 和 WebP 图片")
    return path.as_posix()


def under_root(root: Path, relative: str):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("图片路径超出选择的目录")
    return path


def legacy_relative(item):
    if item.get("relativePath"):
        return relative_path(item["relativePath"])
    url = item.get("imageUrl", "")
    if not isinstance(url, str) or not url.startswith("/imgs/"):
        raise ValueError("缺少 relativePath 或旧版 /imgs/ 图片路径")
    return relative_path(unquote(url[len("/imgs/"):]))


def number(value, name, limit=None):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name}无效")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name}无效") from None
    if not math.isfinite(result) or (limit and abs(result) > limit):
        raise ValueError(f"{name}超出有效范围")
    return result


def coordinates(latitude, longitude):
    lat, lon = number(latitude, "纬度", 90), number(longitude, "经度", 180)
    if (lat is None) != (lon is None):
        raise ValueError("经纬度必须同时存在或同时为空")
    return lat, lon


def normalize_date(value, offset=None):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == ":" and text[7:8] == ":":
        text = text[:10].replace(":", "-") + text[10:]
    try:
        date = datetime.fromisoformat(text.replace("Z", "+00:00"))
        # EXIF normally has no timezone: do not silently mark local time as UTC.
        if date.tzinfo is None and offset:
            date = datetime.fromisoformat(date.isoformat() + str(offset))
        return date.isoformat()
    except ValueError:
        return None


def parse_exif(path):
    with Image.open(path) as image:
        exif = image.getexif()
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo) if exif else {}
        detail = exif.get_ifd(ExifTags.IFD.Exif) if exif else {}

        def dms(tag, ref):
            values = gps.get(tag)
            if not values or len(values) != 3:
                return None
            result = float(values[0]) + float(values[1]) / 60 + float(values[2]) / 3600
            direction = gps.get(ref, "")
            if isinstance(direction, bytes):
                direction = direction.decode("ascii", errors="ignore")
            return -result if direction in ("S", "W") else result

        lat, lon = coordinates(dms(2, 1), dms(4, 3))
        altitude = number(gps.get(6), "高程")
        ref = gps.get(5, 0)
        if altitude is not None and ref in (1, b"\x01"):
            altitude = -abs(altitude)
        captured = normalize_date(
            detail.get(36867) or exif.get(36867) or detail.get(36868) or exif.get(306),
            detail.get(36881) or detail.get(36880))
        return {"latitude": lat, "longitude": lon, "altitude": altitude, "captured_at": captured}


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def thumbnail(source, data_dir, digest):
    relative = f"thumbs/{digest}.v2.webp"
    path = data_dir / relative
    if not path.exists():
        with Image.open(source) as image:
            image.draft("RGB", (128, 128))
            image = ImageOps.exif_transpose(image)
            image.thumbnail((128, 128))
            image = image.convert("RGB")
            temporary = path.with_suffix(".tmp")
            image.save(temporary, "WEBP", quality=20, method=6, exif=b'')
            if temporary.stat().st_size > 4096:
                image.thumbnail((96, 96))
                image.save(temporary, 'WEBP', quality=10, method=6, exif=b'')
            temporary.replace(path)
    return relative
