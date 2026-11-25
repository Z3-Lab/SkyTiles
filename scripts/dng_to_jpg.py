#!/usr/bin/env python3
"""递归解码 DNG，复制并核对 EXIF/GPS/XMP，保留原片及元数据备份。

安装依赖：python -m pip install -r scripts/requirements-dng.txt
运行示例：python scripts/dng_to_jpg.py --limit 2
ExifTool: https://exiftool.org/ （也会自动寻找 Hugin 附带的版本）
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
import sys
import tempfile
from datetime import datetime


DEFAULT_INPUT = Path(r"DNG")
# 这些字段描述输出像素/色彩，必须使用 JPG 实际值，其原值仍保存在备份中。
OUTPUT_TAGS = {"ExifImageWidth", "ExifImageHeight", "ColorSpace"}
IFD0_CHECK = {
    "Make", "Model", "Orientation", "Software", "ModifyDate", "Artist",
    "Copyright", "ImageDescription", "XPTitle", "XPComment", "XPAuthor",
    "XPKeywords", "XPSubject", "CameraSerialNumber", "UniqueCameraModel",
}


class ExifTool:
    def __init__(self, executable: str):
        self.executable = executable

    def run(self, *args: str) -> bytes:
        # Windows 的命令行编码可能破坏中文路径。通过 UTF-8 参数文件传递，
        # 不经过 shell，也不将路径编码成系统 ANSI 编码。
        if any("\n" in a or "\r" in a for a in args):
            raise ValueError("ExifTool 参数不能含换行符")
        env = dict(os.environ, LC_ALL="C", LC_CTYPE="C", LANG="C")
        result = subprocess.run(
            [self.executable, "-charset", "filename=UTF8", "-@", "-"],
            input=("\n".join(args) + "\n").encode("utf-8"),
            capture_output=True, env=env,
        )
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode:
            raise RuntimeError(stderr or result.stdout.decode("utf-8", errors="replace"))
        if stderr:
            print(f"  ExifTool 提示：{stderr}", file=sys.stderr)
        return result.stdout

    def metadata(self, path: Path, *, backup: bool = False) -> dict:
        args = ["-j", "-G0:1:4", "-a", "-s", "-n", "-struct", "-u", "-b"]
        if backup:
            # 原始 XMP/MakerNotes 也按块存储；不备份嵌入的预览图像素。
            args += ["-all", "-MakerNotes", "-XMP", "-ICC_Profile",
                     "--PreviewImage", "--ThumbnailImage", "--JpgFromRaw", "--OtherImage"]
        data = json.loads(self.run(*args, str(path)))
        if len(data) != 1:
            raise RuntimeError(f"读取元数据失败：{path}")
        tags = data[0]
        errors = [v for k, v in tags.items() if k.split(":")[-1] == "Error"]
        if errors:
            raise RuntimeError(str(errors))
        return tags

    def copy(self, source: Path, dest: Path, width: int, height: int) -> None:
        self.run(
            "-n", "-overwrite_original", "-TagsFromFile", str(source),
            # 使用标准写入位置。某些 DJI DNG 将 MakerNotes 放在 IFD0；
            # all:all 加显式 MakerNotes 会在 IFD0/ExifIFD 重复写入，
            # 使 EXIF 超过单段容量，部分摄影测量软件无法读取 GPS。
            "-all", "-EXIF:all", "-MakerNotes", "-XMP",
            # RAW 的相机色彩配置不能用于已转换的 sRGB JPG。
            "--ICC_Profile",
            f"-ExifIFD:ExifImageWidth={width}",
            f"-ExifIFD:ExifImageHeight={height}", "-ExifIFD:ColorSpace=1",
            str(dest),
        )
        # ExifTool 按数值重建 rational 时默认约 8 位精度。直接恢复原始
        # 分子/分母，避免 GPS 高程、焦距、APEX 快门值发生舍入。
        original_exif = self.run("-b", "-EXIF", str(dest))
        patched = restore_rationals(source, original_exif)
        if patched != original_exif:
            exif_path = dest.with_suffix(".exif")
            exif_path.write_bytes(patched)
            try:
                self.run("-overwrite_original", f"-EXIF<={exif_path}", str(dest))
            finally:
                exif_path.unlink(missing_ok=True)


def tiff_rationals(stream) -> tuple[str, dict]:
    """只读取 TIFF 主目录及 EXIF/GPS/Interop 目录，不读取 RAW 像素。"""
    def read_at(offset: int, length: int) -> bytes:
        stream.seek(offset)
        data = stream.read(length)
        if len(data) != length:
            raise ValueError("TIFF/EXIF 数据截断")
        return data

    header = read_at(0, 8)
    endian = {b"II": "<", b"MM": ">"}.get(header[:2])
    if not endian or struct.unpack(endian + "H", header[2:4])[0] != 42:
        raise ValueError("只支持标准 TIFF/DNG，无法无损核对该文件的有理数字段")
    first = struct.unpack(endian + "I", header[4:8])[0]
    rationals = {}
    visited = set()

    def visit(offset: int, group: str) -> None:
        if offset in visited or not offset:
            return
        visited.add(offset)
        count = struct.unpack(endian + "H", read_at(offset, 2))[0]
        for index in range(count):
            entry = read_at(offset + 2 + index * 12, 12)
            tag, kind, length, value_offset = struct.unpack(endian + "HHII", entry)
            if kind in (5, 10):
                if length > 1_000_000:
                    raise ValueError("TIFF rational 数组过大")
                data = read_at(value_offset, length * 8)
                rationals[group, tag] = (kind, length, value_offset, data)
            elif kind == 4 and length == 1 and tag in (34665, 34853, 40965):
                visit(value_offset, {34665: "ExifIFD", 34853: "GPS", 40965: "InteropIFD"}[tag])

    visit(first, "IFD0")
    return endian, rationals


def restore_rationals(source: Path, exif: bytes) -> bytes:
    with source.open("rb") as stream:
        return restore_rationals_from_stream(stream, exif)


def restore_rationals_from_stream(stream, exif: bytes) -> bytes:
    source_endian, source_tags = tiff_rationals(stream)
    dest_endian, dest_tags = tiff_rationals(io.BytesIO(exif))
    result = bytearray(exif)
    for key, (_, count, _, data) in source_tags.items():
        if key not in dest_tags:
            continue  # 仅在 DNG 中适用的字段已存入元数据备份。
        _, dest_count, offset, _ = dest_tags[key]
        if count != dest_count:
            raise ValueError(f"有理数字段长度变化，无法精确复制：{key}")
        if source_endian != dest_endian:
            data = b"".join(data[i:i + 4][::-1] for i in range(0, len(data), 4))
        result[offset:offset + len(data)] = data
    return bytes(result)


def find_exiftool(explicit: str | None) -> str:
    if explicit:
        found = shutil.which(explicit)
        if found:
            return found
        raise RuntimeError(f"找不到 ExifTool：{explicit}")
    candidates = [shutil.which("exiftool"),
                  str(Path(__file__).with_name("exiftool.exe")),
                  r"C:\Program Files\Hugin\bin\exiftool.exe",
                  r"C:\Program Files (x86)\Hugin\bin\exiftool.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("未找到 ExifTool。请从 https://exiftool.org/ 安装，或使用 --exiftool 指定路径。")


def equivalent(a, b) -> bool:
    # 数值序列化时可能存在末位浮点舍入，GPS 仍核对至约微米量级。
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-10)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equivalent(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equivalent(a[k], b[k]) for k in a)
    return a == b


def verify_metadata(source: dict, dest: dict) -> int:
    # CopyN 是 ExifTool 抽取时的实例编号；整理重复 MakerNotes 后会改变。
    # 按实际元数据组和字段名核对，保留所有不同的源值。
    def normalized(key):
        return re.sub(r":Copy\d+(?=:)", "", key)

    dest_values = {}
    for key, value in dest.items():
        dest_values.setdefault(normalized(key), []).append(value)
    failures = []
    checked = 0
    for key, value in source.items():
        parts = key.split(":")
        tag = parts[-1]
        if tag in OUTPUT_TAGS:
            continue
        selected = (
            key.startswith(("EXIF:ExifIFD:", "EXIF:GPS:", "MakerNotes:", "XMP:"))
            or (key.startswith("EXIF:IFD0:") and tag in IFD0_CHECK)
        )
        if selected:
            checked += 1
            candidates = dest_values.get(normalized(key), [])
            if not candidates:
                failures.append(f"{key} 未复制")
            elif not any(equivalent(value, candidate) for candidate in candidates):
                failures.append(f"{key} 值不一致")
    if failures:
        raise RuntimeError("元数据核对失败，未发布 JPG：" + "; ".join(failures))
    return checked


def verify_jpeg_compatibility(path: Path, tool: ExifTool, tags: dict) -> None:
    """除 ExifTool 外，再核对普通单段 EXIF 读取器能否读取 GPS。"""
    import warnings
    from PIL import Image

    if len(tool.run("-b", "-EXIF", str(path))) > 65527:
        raise RuntimeError("EXIF 超过标准单个 APP1 段容量，可能无法被 RealityScan 读取")
    source_has_gps = any(k.startswith("EXIF:GPS:") and k.endswith(":GPSLatitude") for k in tags)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with Image.open(path) as image:
            if source_has_gps:
                gps = image.getexif().get_ifd(34853)
                if not all(k in gps for k in (1, 2, 3, 4)):
                    raise RuntimeError("标准 EXIF 读取器无法读取完整 GPS 经纬度")


def output_path(source: Path, root: Path, output: Path) -> Path:
    return output / source.relative_to(root).with_suffix(".jpg")


def convert_one(source: Path, dest: Path, tool: ExifTool, quality: int) -> int:
    import rawpy
    from PIL import Image, ImageCms

    source_stat = source.stat()
    original = tool.metadata(source, backup=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup_path = dest.with_suffix(".dng-metadata.json")
    # 备份先落盘，即使解码或元数据复制失败，也留下原始元数据供检查。
    backup = {
        "source": str(source),
        "note": "ExifTool metadata with base64 binary values; embedded previews excluded. Keep the original DNG.",
        "metadata": original,
    }
    # 同目录临时文件保证最终发布使用原子重命名，失败时没有半成品 JPG。
    with tempfile.TemporaryDirectory(prefix=".dng-convert-", dir=dest.parent) as temp:
        temp_dir = Path(temp)
        temp_backup = temp_dir / "metadata.json"
        temp_backup.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_backup, backup_path)
        temp_jpg = temp_dir / "image.jpg"
        # 使用文件对象兼容中文路径。禁止旋转像素，保留原始 Orientation，
        # 防止解码时已旋转、复制 EXIF 后显示软件又旋转一次。
        with source.open("rb") as stream, rawpy.imread(stream) as raw:
            rgb = raw.postprocess(use_camera_wb=True, output_color=rawpy.ColorSpace.sRGB,
                                  output_bps=8, user_flip=0, no_auto_bright=True)
        with Image.fromarray(rgb) as image:
            width, height = image.size
            srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            image.save(temp_jpg, "JPEG", quality=quality, subsampling=0, icc_profile=srgb)
        del rgb
        tool.copy(source, temp_jpg, width, height)
        # 备份中含原始块数据，校验读取常规逐项标签即可。
        source_tags = tool.metadata(source)
        dest_tags = tool.metadata(temp_jpg)
        checked = verify_metadata(source_tags, dest_tags)
        verify_jpeg_compatibility(temp_jpg, tool, source_tags)
        with Image.open(temp_jpg) as image:
            image.verify()
        if dest.exists():
            raise FileExistsError(f"输出已存在，不覆盖：{dest}")
        os.utime(temp_jpg, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        # Windows rename 不覆盖目标；对其他平台也在发布前检查了目标是否存在。
        temp_jpg.rename(dest)
    return checked


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT, help="DNG 所在目录（递归）")
    parser.add_argument("-o", "--output", type=Path, help="输出目录，默认是输入目录旁的 <目录名>_JPG")
    parser.add_argument("--quality", type=int, choices=range(1, 101), default=95, metavar="1-100")
    parser.add_argument("--limit", type=positive, help="仅处理前 N 张，适合试转")
    parser.add_argument("--exiftool", help="ExifTool 可执行文件路径")
    parser.add_argument("--dry-run", action="store_true", help="仅列出任务，不写入文件")
    args = parser.parse_args(argv)
    root = args.input.resolve()
    if not root.is_dir():
        parser.error(f"输入目录不存在：{root}")
    output = (args.output or root.with_name(root.name + "_JPG")).resolve()
    if output == root:
        parser.error("输出目录应与输入目录不同，以便保护原片和已有 JPG")
    files = sorted((p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".dng"),
                   key=lambda p: str(p).casefold())
    # --limit 作用于待转换文件，重跑时跳过已有文件后继续处理。
    pending = [p for p in files if not output_path(p, root, output).exists()]
    skipped = len(files) - len(pending)
    if args.limit:
        pending = pending[:args.limit]
    print(f"原片：{root}\n输出：{output}")
    print(f"共 {len(files)} 张 DNG，已有 JPG 跳过 {skipped} 张，本次转换 {len(pending)} 张。", flush=True)
    if not pending:
        return 0
    if args.dry_run:
        for source in pending:
            print(f"{source} -> {output_path(source, root, output)}")
        return 0
    try:
        import rawpy  # noqa: F401
        from PIL import Image, ImageCms  # noqa: F401
        tool = ExifTool(find_exiftool(args.exiftool))
        print(f"ExifTool {tool.run('-ver').decode().strip()}", flush=True)
    except (ImportError, OSError, RuntimeError) as exc:
        print(f"依赖检查失败：{exc}\n请用当前 Python 安装依赖：\n"
              f'  "{sys.executable}" -m pip install -r "{Path(__file__).with_name("requirements-dng.txt")}"', file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / ("conversion-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl")
    failed = 0
    completed = 0
    try:
        with report_path.open("x", encoding="utf-8") as report:
            for index, source in enumerate(pending, 1):
                dest = output_path(source, root, output)
                print(f"[{index}/{len(pending)}] {source.relative_to(root)}", flush=True)
                row = {"source": str(source), "output": str(dest)}
                try:
                    count = convert_one(source, dest, tool, args.quality)
                    completed += 1
                    row.update(status="ok", metadata_tags_checked=count)
                    print(f"  完成，已核对 {count} 个 EXIF/GPS/XMP/厂家字段。", flush=True)
                except Exception as exc:
                    failed += 1
                    row.update(status="failed", error=str(exc))
                    print(f"  失败：{exc}", file=sys.stderr, flush=True)
                report.write(json.dumps(row, ensure_ascii=False) + "\n")
                report.flush()
    except KeyboardInterrupt:
        print("\n已停止；再次运行会跳过已有 JPG。", file=sys.stderr)
        return 130
    print(f"完成 {completed} 张，失败 {failed} 张。日志：{report_path}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    raise SystemExit(main())
