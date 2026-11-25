# DNG 批量转 JPG

脚本：`dng_to_jpg.py`。需要 Python 3.10 或更高版本、rawpy、Pillow、ExifTool。
当前电脑会自动使用 `C:\Program Files\Hugin\bin\exiftool.exe`。

## 安装和运行

在 PowerShell 中执行以下命令。依赖放在独立环境中，不修改已有项目的 Python 依赖：

```powershell
cd '<司图项目目录>'
python -m venv .dng-jpg-venv
.\.dng-jpg-venv\Scripts\python.exe -m pip install -r .\scripts\requirements-dng.txt
```

先试转两张：

```powershell
.\.dng-jpg-venv\Scripts\python.exe .\scripts\dng_to_jpg.py --limit 2
```

确认效果后，转换全部照片（自动跳过前面已经生成的 JPG）：

```powershell
.\.dng-jpg-venv\Scripts\python.exe .\scripts\dng_to_jpg.py
```

默认输入：`DNG`。

默认输出：`DNG_JPG`。

也可以指定输入和输出；支持空格、中文路径、大小写混合的 `.DNG` 扩展名：

```powershell
.\.dng-jpg-venv\Scripts\python.exe .\scripts\dng_to_jpg.py 'E:\其他原片目录' -o 'E:\JPG输出目录'
```

其他参数：

- `--dry-run`：只查看文件清单，不创建文件；不需要图像解码依赖。
- `--limit 10`：跳过已有 JPG 后，最多处理 10 张待转换照片。
- `--quality 98`：JPG 质量，1–100，默认 95；全分辨率、4:4:4 采样。
- `--exiftool 'D:\工具\exiftool.exe'`：手动指定 ExifTool。

## EXIF 和元数据处理

- 递归遍历子目录，在输出中保留相同的目录结构；不改写、删除原始 DNG。
- 从 RAW 数据解码全尺寸图像，不使用 DNG 内置的低分辨率预览图。使用拍摄时白平衡、sRGB 输出，关闭逐图自动提亮。
- 使用 ExifTool 复制可写标签，并整块复制 XMP 和 MakerNotes；包含拍摄时间、相机/镜头、曝光参数、GPS、DJI RTK、飞行/云台姿态、相机标定等源文件中存在的信息。整块复制 XMP 也保留无法逐项写入的自定义 XMP 标签。
- 使用原片的有理数分子/分母恢复 JPG 中对应字段，避免常规复制中的数值舍入，尤其是 GPS 高程、焦距和 APEX 快门值。
- 保留原始 Orientation 标签，解码时不额外旋转像素，避免显示时旋转两次。
- JPG 的 EXIF 宽高和色彩空间使用实际输出值，并嵌入 sRGB 配置。RAW 相机色彩配置不应用于转换后的 JPG。XMP 原始块保持原样，因此其中的 `dc:format` 等可能仍描述原始 DNG。
- 每张图旁保存 `.dng-metadata.json`，包含 ExifTool 提取的源元数据，以及原始 XMP/MakerNotes 块等二进制信息（base64）。内嵌预览图像素不进入备份。JSON 是补充备份，不是 DNG 原片的替代品。
- 输出前逐项核对 ExifIFD 拍摄字段、GPS、MakerNotes、XMP 及主要 IFD0 身份/方向字段。输出宽高和色彩空间除外。核对失败时不发布 JPG，保留备份并将错误记录在日志中。
- 同时检查 EXIF 是否适合标准单个 APP1 段，并用 Pillow 独立验证 GPS 目录可读，避免只通过 ExifTool 校验、摄影测量软件却无法读取定位的情况。
- 文件夹里生成 `conversion-日期时间.jsonl`，逐张记录成功/失败及核对字段数量。单张失败后继续处理下一张；存在失败时程序退出码为 1。已有 JPG 直接跳过，不重新转换或核对。

**格式限制：**JPG 是有损的 8 位成片，无法替代 RAW，也无法表达所有 DNG 原始传感器/显影专用信息。脚本会备份这些可提取信息，原始 DNG 必须保留。

早期版本使用 `-all:all` 保留原始标签位置，再显式复制 MakerNotes。部分 DJI DNG 把 MakerNotes 放在非标准的 IFD0，导致同一厂家信息在 IFD0 与 ExifIFD 中重复写入，生成多段 EXIF。一些 JPG 的 GPS 目录因此位于第二段，ExifTool 虽可读取，普通读取器却可能得到空 GPS，影响 RealityScan 地理参考。现已改用标准写入位置，单独复制 EXIF 及 XMP 块，并加入普通读取器检查。旧版已生成文件不会自动变化，需单独整理元数据，无须重新压缩图片像素。

ExifTool 也可能提示原片的 MakerNotes 无法重建；脚本会保留原始备份并核对输出中可读取的厂家字段，出现字段缺失或值不一致时会判为失败。

参考：[ExifTool 标签复制](https://exiftool.org/exiftool_pod.html#COPYING-EXAMPLES)、[元数据备份及块复制](https://exiftool.org/metafiles.html)、[rawpy 解码参数](https://letmaik.github.io/rawpy/api/rawpy.Params.html)。
