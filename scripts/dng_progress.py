#!/usr/bin/env python3
"""显示现有 DNG 转换任务的实时进度；关闭窗口不会停止转换。"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import math
from pathlib import Path
import re
import time
import tkinter as tk
from tkinter import ttk


def process_running(pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def duration(seconds: float) -> str:
    minutes = max(0, math.ceil(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"


class ProgressWindow:
    def __init__(self, args):
        self.args = args
        self.offset = 0
        self.partial = b""
        self.ok = 0
        self.failed = 0
        self.started = args.report.stat().st_ctime
        self.window = tk.Tk()
        self.window.title(args.title)
        self.window.geometry("820x510")
        self.window.minsize(760, 480)
        self.window.configure(bg="#f5f7fb")
        style = ttk.Style(self.window)
        style.theme_use("clam")
        style.configure("Conversion.Horizontal.TProgressbar", troughcolor="#e1e7f0",
                        background="#3273dc", borderwidth=0, thickness=18)
        panel = tk.Frame(self.window, bg="#f5f7fb", padx=32, pady=24)
        panel.pack(fill="both", expand=True)

        def label(text="", size=12, color="#25334a", **kwargs):
            item = tk.Label(panel, text=text, bg="#f5f7fb", fg=color,
                            font=("Microsoft YaHei UI", size), anchor="w", **kwargs)
            item.pack(fill="x", pady=5)
            return item

        label(args.title, 22)
        self.status = label("正在读取转换进度…", 12, "#52627b")
        self.count = label("", 30)
        self.bar = ttk.Progressbar(panel, style="Conversion.Horizontal.TProgressbar",
                                   maximum=args.total)
        self.bar.pack(fill="x", pady=(12, 14))
        self.detail = label("", 12)
        self.estimate = label("", 12)
        self.current = label("", 10, "#52627b", wraplength=740, justify="left")
        label("输出：" + str(args.report.parent), 10, "#52627b", wraplength=740, justify="left")
        self.updated = label("", 10, "#738198")
        label("每 2 秒自动刷新 · 关闭此窗口后，转换仍会继续", 10, "#738198")
        self.window.lift()
        self.window.attributes("-topmost", True)
        self.window.after(1800, lambda: self.window.attributes("-topmost", False))
        self.refresh()

    def read_report(self):
        with self.args.report.open("rb") as stream:
            stream.seek(self.offset)
            data = self.partial + stream.read()
            self.offset = stream.tell()
        lines = data.split(b"\n")
        self.partial = lines.pop()
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") == "ok":
                self.ok += 1
            elif row.get("status") == "failed":
                self.failed += 1

    def refresh(self):
        try:
            self.read_report()
            running = process_running(self.args.pid)
            done = self.ok + self.failed
            percent = 100 * done / self.args.total
            self.bar["value"] = done
            self.count.config(text=f"{done:,} / {self.args.total:,}    {percent:.1f}%")
            self.detail.config(text=f"成功并通过元数据核对：{self.ok:,} 张     失败：{self.failed:,} 张")
            elapsed = time.time() - self.started
            if running:
                self.status.config(text="正在处理 · EXIF / GPS / DJI 信息逐张核对", fg="#3273dc")
                remaining = elapsed / done * max(0, self.args.total - done) if done else 0
                eta = datetime.fromtimestamp(time.time() + remaining).strftime("%H:%M")
                self.estimate.config(text=f"已运行 {duration(elapsed)} · 预计还需 {duration(remaining)}（约 {eta} 完成）")
            elif done >= self.args.total:
                self.status.config(text="转换完成" if not self.failed else "转换结束，部分文件失败，请查看日志",
                                   fg="#21844a" if not self.failed else "#b36a16")
                self.estimate.config(text=f"转换已结束 · 成功 {self.ok:,} 张，失败 {self.failed:,} 张")
            else:
                self.status.config(text="转换进程已停止，任务尚未全部完成，请检查日志", fg="#b34737")
                self.estimate.config(text=f"尚有 {max(0, self.args.total - done):,} 张未处理")
            with self.args.stdout.open("rb") as stream:
                stream.seek(max(0, self.args.stdout.stat().st_size - 12000))
                matches = re.findall(r"\[\d+/\d+\] ([^\r\n]+)", stream.read().decode("utf-8", errors="replace"))
            self.current.config(text=("当前文件：" if running else "最后处理：") + (matches[-1] if matches else "准备中"))
            self.updated.config(text="最后更新：" + datetime.now().strftime("%H:%M:%S"))
            if self.args.status_file:
                self.args.status_file.write_text(json.dumps({
                    "window_pid": __import__("os").getpid(), "conversion_pid": self.args.pid,
                    "running": running, "success": self.ok, "failed": self.failed,
                    "total": self.args.total, "updated": datetime.now().isoformat(),
                }), encoding="utf-8")
        except (OSError, ValueError) as exc:
            self.status.config(text=f"读取进度失败，将自动重试：{exc}", fg="#b36a16")
        self.window.after(2000, self.refresh)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--stdout", required=True, type=Path)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--total", required=True, type=int)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--title", default="DNG → JPG 转换进度")
    args = parser.parse_args()
    ProgressWindow(args).window.mainloop()


if __name__ == "__main__":
    main()
