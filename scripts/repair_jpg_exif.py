#!/usr/bin/env python3
"""修复旧版 DNG 转换生成的多段 EXIF；另存 JPG，不重新压缩图像。"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile

from dng_to_jpg import (ExifTool, find_exiftool, restore_rationals_from_stream,
                        verify_jpeg_compatibility, verify_metadata)


def jpeg_info(path: Path, *, hash_data: bool = True) -> tuple[int, str]:
    """计数 EXIF 段；对除 APP/COM 元数据外的 JPEG 编码数据计算校验值。"""
    digest = hashlib.sha256()
    exif_segments = 0
    with path.open("rb") as stream:
        soi = stream.read(2)
        if soi != b"\xff\xd8":
            raise ValueError(f"不是 JPEG 文件：{path}")
        digest.update(soi)
        while True:
            marker = stream.read(2)
            if len(marker) != 2 or marker[0] != 255:
                raise ValueError("JPEG 段头损坏")
            if marker == b"\xff\xd9":
                digest.update(marker)
                break
            length_bytes = stream.read(2)
            if len(length_bytes) != 2:
                raise ValueError("JPEG 长度字段截断")
            length = struct.unpack(">H", length_bytes)[0]
            if length < 2:
                raise ValueError("JPEG 段长度无效")
            payload = stream.read(length - 2)
            if len(payload) != length - 2:
                raise ValueError("JPEG 数据截断")
            if marker == b"\xff\xe1" and payload.startswith(b"Exif\0\0"):
                exif_segments += 1
            if not (224 <= marker[1] <= 239 or marker[1] == 254):
                digest.update(marker + length_bytes + payload)
            if marker == b"\xff\xda":
                if hash_data:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                break
    return exif_segments, digest.hexdigest()


def repair_one(source: Path, dest: Path, tool: ExifTool) -> dict:
    original_stat = source.stat()
    before_segments, before_hash = jpeg_info(source)
    source_tags = tool.metadata(source)
    source_exif = tool.run("-b", "-EXIF", str(source))
    source_xmp = tool.run("-b", "-XMP", str(source))
    source_icc = tool.run("-b", "-ICC_Profile", str(source))
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".exif-repair-", dir=dest.parent) as temp:
        working = Path(temp) / "image.jpg"
        shutil.copy2(source, working)
        # 重建标准目录，避免 DJI MakerNotes 在 IFD0 与 ExifIFD 中各存一份。
        # EXIF 显式复制以防同名 XMP 字段的优先级盖过拍摄参数。
        tool.run("-n", "-overwrite_original", "-all=", "-TagsFromFile", str(source),
                 "-all", "-EXIF:all", "-MakerNotes", "-XMP", "-ICC_Profile", str(working))
        compact_exif = tool.run("-b", "-EXIF", str(working))
        exact_exif = restore_rationals_from_stream(io.BytesIO(source_exif), compact_exif)
        if exact_exif != compact_exif:
            block = Path(temp) / "metadata.exif"
            block.write_bytes(exact_exif)
            tool.run("-overwrite_original", f"-EXIF<={block}", str(working))
        dest_tags = tool.metadata(working)
        checked = verify_metadata(source_tags, dest_tags)
        verify_jpeg_compatibility(working, tool, source_tags)
        after_segments, after_hash = jpeg_info(working)
        if after_segments != 1 or after_hash != before_hash:
            raise RuntimeError("EXIF 段数或 JPEG 图像压缩数据核对失败")
        if source_xmp != tool.run("-b", "-XMP", str(working)):
            raise RuntimeError("DJI XMP 原始块不一致")
        if source_icc != tool.run("-b", "-ICC_Profile", str(working)):
            raise RuntimeError("ICC 色彩配置不一致")
        validation = tool.run("-s3", "-validate", str(working)).decode().strip()
        if validation != "OK":
            raise RuntimeError(f"EXIF 结构检查未通过：{validation}")
        if source.stat().st_size != original_stat.st_size or source.stat().st_mtime_ns != original_stat.st_mtime_ns:
            raise RuntimeError("处理期间源文件发生变化，请重试")
        if dest.exists():
            raise FileExistsError(f"输出已存在，不覆盖：{dest}")
        os.utime(working, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        working.rename(dest)
    return {"metadata_tags_checked": checked, "source_exif_segments": before_segments,
            "output_exif_segments": after_segments, "jpeg_data_sha256": after_hash,
            "jpeg_data_unchanged": True, "xmp_unchanged": True, "icc_unchanged": True,
            "standard_gps_reader_ok": True, "exif_validate": validation}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = args.input.resolve()
    output = args.output.resolve()
    if not root.is_dir() or output == root or root in output.parents:
        parser.error("输入必须是目录，输出必须是输入目录之外的独立文件夹")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg"))
    affected = [p for p in files if jpeg_info(p, hash_data=False)[0] > 1]
    pending = [p for p in affected if not (output / p.relative_to(root)).exists()]
    skipped = len(affected) - len(pending)
    if args.limit:
        pending = pending[:args.limit]
    print(f"原 JPG：{root}\n修复输出：{output}", flush=True)
    print(f"扫描 {len(files)} 张 JPG，多段 EXIF {len(affected)} 张；已有输出跳过 {skipped} 张；本次处理 {len(pending)} 张。", flush=True)
    if args.dry_run or not pending:
        return 0
    tool = ExifTool(find_exiftool(None))
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / ("repair-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl")
    failed = 0
    completed = 0
    try:
        with report_path.open("x", encoding="utf-8") as report:
            for index, source in enumerate(pending, 1):
                dest = output / source.relative_to(root)
                print(f"[{index}/{len(pending)}] {source.relative_to(root)}", flush=True)
                row = {"source": str(source), "output": str(dest)}
                try:
                    row.update(repair_one(source, dest, tool), status="ok")
                    completed += 1
                    print(f"  完成；GPS 可读，图片编码数据完全一致，核对 {row['metadata_tags_checked']} 个字段。", flush=True)
                except Exception as exc:
                    row.update(status="failed", error=str(exc))
                    failed += 1
                    print(f"  失败：{exc}", file=sys.stderr, flush=True)
                report.write(json.dumps(row, ensure_ascii=False) + "\n")
                report.flush()
    except KeyboardInterrupt:
        print("已停止；原 JPG 不受影响，重跑会跳过已输出文件。", flush=True)
        return 130
    print(f"完成 {completed} 张，失败 {failed} 张。日志：{report_path}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
