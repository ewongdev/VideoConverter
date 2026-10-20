#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频格式转换工具 v1.1（重构版）
- 真正生效的 NVIDIA / AMD / Intel 硬件编码（启动时自动检测）
- 硬件解码 + 硬件编码，失败自动降级
- 实时进度 / 速度 / 剩余时间
- 线程安全 UI
"""
import os
import re
import sys
import time
import queue
import shutil
import threading
import subprocess
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ======================= 常量 =======================
KEEP = "原视频"

SUPPORTED_EXTS = {
    ".mpg", ".mpeg", ".avi", ".mov", ".wmv", ".flv", ".f4v", ".mkv",
    ".mp4", ".m4v", ".rm", ".rmvb", ".vob", ".ts", ".m2ts", ".mts",
    ".webm", ".3gp", ".3g2", ".ogv", ".dv", ".divx", ".asf", ".mxf",
}

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 后端 -> 编码器
ENCODERS = {
    "nvidia": {"h264": "h264_nvenc", "h265": "hevc_nvenc"},
    "amd":    {"h264": "h264_amf",   "h265": "hevc_amf"},
    "intel":  {"h264": "h264_qsv",   "h265": "hevc_qsv"},
    "cpu":    {"h264": "libx264",    "h265": "libx265"},
}
BACKEND_NAME = {"nvidia": "NVIDIA(NVENC)", "amd": "AMD(AMF)",
                "intel": "Intel(QSV)", "cpu": "CPU(软件)"}

# 编码速度预设： 后端 -> {档位: preset}
SPEED_PRESET = {
    "nvidia": {"fast": "p2", "balanced": "p4", "quality": "p6"},
    "amd":    {"fast": "speed", "balanced": "balanced", "quality": "quality"},
    "intel":  {"fast": "veryfast", "balanced": "medium", "quality": "slow"},
    "cpu":    {"fast": "veryfast", "balanced": "fast", "quality": "medium"},
}

HW_LABELS = {
    "自动": "auto",
    "NVIDIA 显卡 (NVENC)": "nvidia",
    "AMD 显卡 (AMF)": "amd",
    "Intel 核显 (QSV)": "intel",
    "CPU (软件编码，最慢)": "cpu",
}
SPEED_LABELS = {"速度优先": "fast", "均衡": "balanced", "质量优先": "quality"}
RES_LABELS = ["480p", "720p", "1080p", "2k", "4k"]
LONG_EDGE = {"480p": 854, "720p": 1280, "1080p": 1920, "2k": 2560, "4k": 3840}

# 各容器可直接复制(copy)的编码
VIDEO_COPY_OK = {
    ".mp4": {"h264", "hevc", "mpeg4", "mpeg2video", "av1", "vp9"},
    ".mov": {"h264", "hevc", "mpeg4", "mjpeg", "prores", "mpeg2video"},
    ".mkv": None,  # None = 全部支持
}
AUDIO_COPY_OK = {
    ".mp4": {"aac", "mp3", "ac3", "eac3", "alac"},
    ".mov": {"aac", "mp3", "ac3", "alac", "pcm_s16le", "pcm_s24le", "pcm_s16be"},
    ".mkv": None,
}


# ======================= 工具函数 =======================
def find_ffmpeg():
    """查找顺序：打包内嵌 -> exe 同目录 -> 脚本目录 -> 系统 PATH"""
    dirs = []
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            dirs.append(sys._MEIPASS)
        dirs.append(os.path.dirname(sys.executable))
    else:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    for d in dirs:
        for name in ("ffmpeg.exe", "ffmpeg"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return shutil.which("ffmpeg") or "ffmpeg"


def run_quiet(cmd, timeout=30):
    """运行命令并返回 (returncode, stdout+stderr 文本)"""
    try:
        r = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode, r.stdout or ""
    except FileNotFoundError:
        return -1, "未找到 ffmpeg"
    except subprocess.TimeoutExpired:
        return -2, "命令超时"
    except Exception as e:  # noqa
        return -3, str(e)


def fmt_time(sec):
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def last_error_lines(text, n=3):
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    return " | ".join(lines[-n:]) if lines else "未知错误"


# ======================= 媒体信息 =======================
@dataclass
class MediaInfo:
    duration: float = 0.0
    has_video: bool = False
    vcodec: str = ""
    width: int = 0
    height: int = 0
    vbitrate: int = 0        # kbps
    total_bitrate: int = 0   # kbps
    fps: float = 0.0
    acodecs: list = field(default_factory=list)


def parse_info(text):
    info = MediaInfo()
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    if m:
        info.total_bitrate = int(m[1])
    for line in text.splitlines():
        if "Stream #" not in line:
            continue
        if " Video:" in line and "attached pic" not in line and not info.has_video:
            info.has_video = True
            m = re.search(r"Video:\s*([\w\-]+)", line)
            info.vcodec = m[1].lower() if m else ""
            m = re.search(r",\s*(\d{2,5})x(\d{2,5})", line)
            if m:
                info.width, info.height = int(m[1]), int(m[2])
            m = re.search(r"(\d+)\s*kb/s", line)
            if m:
                info.vbitrate = int(m[1])
            m = re.search(r"([\d.]+)\s*fps", line)
            if m:
                try:
                    info.fps = float(m[1])
                except ValueError:
                    pass
        elif " Audio:" in line:
            m = re.search(r"Audio:\s*([\w\-]+)", line)
            if m:
                info.acodecs.append(m[1].lower())
    return info


# ======================= 转换参数快照 =======================
@dataclass
class Options:
    fast_copy: bool
    ext: str
    codec: str          # h264 / h265
    hw: str             # auto / nvidia / amd / intel / cpu
    hwdec: bool
    speed: str          # fast / balanced / quality
    long_edge: int      # 0 = 保持
    vbitrate: int       # 0 = 保持
    fps: str            # "" = 保持
    abitrate: str       # "" = 保持，如 "192k"
    ar: str             # "" = 保持，如 "44100"
    codec_tag_hvc1: bool = False   # 极速模式下 HEVC 封装进 mp4/mov 需要 hvc1 标签


# ======================= 命令构建 =======================
def decide_bitrate(opts, info):
    """返回目标视频码率(kbps)，0 表示使用恒定质量模式"""
    if opts.vbitrate:
        return opts.vbitrate
    base = info.vbitrate or int(info.total_bitrate * 0.9)
    if not base:
        return 0
    src_long = max(info.width, info.height)
    if opts.long_edge and src_long:
        base *= min(1.0, (opts.long_edge / src_long) ** 2)
    is_src_hevc = info.vcodec in ("hevc", "h265")
    if opts.codec == "h265" and not is_src_hevc:
        base *= 0.65
    elif opts.codec == "h264" and is_src_hevc:
        base *= 1.4
    return int(max(base, 500))


def video_args(backend, opts, info):
    enc = ENCODERS[backend][opts.codec]
    preset = SPEED_PRESET[backend][opts.speed]
    b = decide_bitrate(opts, info)
    args = ["-c:v", enc]

    if backend == "nvidia":
        args += ["-preset", preset, "-rc", "vbr"]
        if b:
            args += ["-b:v", f"{b}k", "-maxrate", f"{int(b * 1.5)}k", "-bufsize", f"{b * 2}k"]
        else:
            args += ["-cq", "23", "-b:v", "0"]
        args += ["-pix_fmt", "yuv420p"]
    elif backend == "amd":
        args += ["-usage", "transcoding", "-quality", preset]
        if b:
            args += ["-rc", "vbr_peak", "-b:v", f"{b}k", "-maxrate", f"{int(b * 1.5)}k"]
        else:
            args += ["-rc", "cqp", "-qp_i", "23", "-qp_p", "25"]
        args += ["-pix_fmt", "yuv420p"]
    elif backend == "intel":
        args += ["-preset", preset]
        if b:
            args += ["-b:v", f"{b}k", "-maxrate", f"{int(b * 1.5)}k"]
        else:
            args += ["-global_quality", "23"]
        args += ["-pix_fmt", "nv12"]
    else:  # cpu
        args += ["-preset", preset]
        if b:
            args += ["-b:v", f"{b}k", "-maxrate", f"{int(b * 1.5)}k", "-bufsize", f"{b * 2}k"]
        else:
            args += ["-crf", "20" if opts.codec == "h264" else "24"]
        args += ["-pix_fmt", "yuv420p"]
        if opts.codec == "h265":
            args += ["-x265-params", "log-level=error"]

    if opts.codec == "h265" and opts.ext in (".mp4", ".mov"):
        args += ["-tag:v", "hvc1"]

    if opts.long_edge:
        e = opts.long_edge
        args += ["-vf", f"scale='if(gt(iw,ih),{e},-2)':'if(gt(iw,ih),-2,{e})'"]
    if opts.fps:
        args += ["-r", opts.fps]
    return args


def audio_args(opts, info):
    if not info.acodecs:
        return []
    allowed = AUDIO_COPY_OK.get(opts.ext)
    compat = allowed is None or all(c in allowed for c in info.acodecs)
    if not opts.abitrate and not opts.ar and compat:
        return ["-c:a", "copy"]
    args = ["-c:a", "aac", "-b:a", opts.abitrate or "192k"]
    if opts.ar:
        args += ["-ar", opts.ar]
    return args


def build_cmd(ffmpeg, src, dst, opts, info, backend, hwdec):
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats"]
    if not opts.fast_copy and hwdec and backend != "cpu":
        cmd += ["-hwaccel", "auto"]
    cmd += ["-i", str(src), "-map", "0:v:0", "-map", "0:a?",
            "-sn", "-dn", "-map_metadata", "0"]

    if opts.fast_copy:
        cmd += ["-c:v", "copy"]
        if opts.codec_tag_hvc1:
            cmd += ["-tag:v", "hvc1"]
        cmd += audio_args(replace(opts, abitrate="", ar=""), info)
    else:
        cmd += video_args(backend, opts, info)
        cmd += audio_args(opts, info)

    if opts.ext in (".mp4", ".mov"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(dst))
    return cmd


# ======================= 主程序 =======================
class VideoConverterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("视频格式转换工具")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1280, sw - 60), min(760, sh - 80)
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}")
        root.minsize(950, 560)

        self.ffmpeg = find_ffmpeg()
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.proc = None
        self.proc_lock = threading.Lock()
        self.enc_cache = {}
        self.enc_lock = threading.Lock()
        self.files = []
        self.running = False

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._poll)

        # 启动时日志保持为空，只在后台检测硬件并更新“硬件编码检测”一行
        threading.Thread(target=self._detect_hw, daemon=True).start()

    # ---------------- UI ----------------
    def _build_ui(self):
        style = ttk.Style()
        for t in ("vista", "clam"):
            if t in style.theme_names():
                style.theme_use(t)
                break
        df = ("Microsoft YaHei UI", 9)
        bf = ("Microsoft YaHei UI", 9, "bold")
        style.configure(".", font=df)
        style.configure("TLabelframe.Label", font=bf, foreground="#1f2937")
        style.configure("TButton", padding=(6, 3))
        style.configure("Action.TButton", font=bf, padding=(10, 5))
        style.configure("Fast.TCheckbutton", font=bf, foreground="#0066cc")

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, sashwidth=6,
                               sashrelief=tk.RAISED, bg="#dcdcdc", bd=0, opaqueresize=True)
        paned.grid(row=0, column=0, sticky="nsew", padx=12, pady=(10, 4))

        # ---- 左：文件列表 ----
        left = ttk.Frame(paned)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        ff = ttk.LabelFrame(left, text=" 1. 待处理的视频文件 ", padding=10)
        ff.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        ff.columnconfigure(0, weight=1)
        ff.rowconfigure(1, weight=1)

        bar = ttk.Frame(ff)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for i in range(4):
            bar.columnconfigure(i, weight=1)
        ttk.Button(bar, text="➕ 添加视频文件", command=self.add_files).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(bar, text="📁 添加文件夹", command=self.add_folder).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(bar, text="➖ 移除选中", command=self.remove_selected).grid(row=0, column=2, sticky="ew", padx=4)
        ttk.Button(bar, text="🗑 清空列表", command=self.clear_list).grid(row=0, column=3, sticky="ew", padx=(4, 0))

        lf = ttk.Frame(ff)
        lf.grid(row=1, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(lf, selectmode=tk.EXTENDED, font=df, relief="solid", bd=1,
                                  highlightthickness=0, selectbackground="#0078D7",
                                  selectforeground="white", activestyle="none")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sy = ttk.Scrollbar(lf, orient="vertical", command=self.listbox.yview)
        sy.grid(row=0, column=1, sticky="ns")
        sx = ttk.Scrollbar(lf, orient="horizontal", command=self.listbox.xview)
        sx.grid(row=1, column=0, sticky="ew")
        self.listbox.config(yscrollcommand=sy.set, xscrollcommand=sx.set)

        # ---- 右 ----
        right = ttk.Frame(paned)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        paned.add(left, width=620, minsize=300, stretch="always")
        paned.add(right, minsize=520, stretch="always")

        pf = ttk.LabelFrame(right, text=" 2. 参数设置 ", padding=(12, 8, 12, 10))
        pf.grid(row=0, column=0, sticky="ew", padx=(4, 0), pady=(0, 8))
        pf.columnconfigure(1, weight=1)
        pf.columnconfigure(3, weight=1)

        self.fast_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pf, text="⚡ 极速无损模式 (仅换封装格式，不重新编码，速度最快)",
                        variable=self.fast_var, command=self.toggle_fast,
                        style="Fast.TCheckbutton").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        ttk.Separator(pf, orient="horizontal").grid(row=1, column=0, columnspan=4, sticky="ew", pady=(2, 8))

        def combo(row, col, label, var, values, readonly=True):
            ttk.Label(pf, text=label).grid(row=row, column=col, sticky="w", pady=5)
            cb = ttk.Combobox(pf, textvariable=var, values=values, width=24,
                              state="readonly" if readonly else "normal")
            cb.grid(row=row, column=col + 1, sticky="ew", padx=(6, 14 if col == 0 else 0), pady=5)
            return cb

        self.format_var = tk.StringVar(value=".mp4")
        self.format_combo = combo(2, 0, "目标文件格式:", self.format_var, [".mp4", ".mkv", ".mov"])

        self.codec_var = tk.StringVar(value="H.264 (兼容性最好)")
        self.codec_combo = combo(2, 2, "视频压缩编码:", self.codec_var,
                                 ["H.264 (兼容性最好)", "H.265 (体积更小/兼容性稍差)"])

        self.hw_var = tk.StringVar(value="自动")
        self.hw_combo = combo(3, 0, "硬件加速方式:", self.hw_var, list(HW_LABELS))

        self.res_var = tk.StringVar(value=KEEP)
        self.res_combo = combo(3, 2, "画面尺寸:", self.res_var, [KEEP] + RES_LABELS)

        self.vbr_var = tk.StringVar(value=KEEP)
        self.vbr_combo = combo(4, 0, "视频码率(kbps):", self.vbr_var,
                               [KEEP, "1500", "2000", "3000", "4000", "5000", "8000", "10000"],
                               readonly=False)
        # 点击 / 获得焦点时自动全选，方便直接输入新数值
        self.vbr_combo.bind("<Button-1>", self.select_all_bitrate)
        self.vbr_combo.bind("<FocusIn>", self.select_all_bitrate)

        self.fps_var = tk.StringVar(value=KEEP)
        self.fps_combo = combo(4, 2, "画面帧率(FPS):", self.fps_var, [KEEP, "25", "30", "60"])

        self.abr_var = tk.StringVar(value=KEEP)
        self.abr_combo = combo(5, 0, "音频质量(码率):", self.abr_var,
                               [KEEP, "128 kbps", "192 kbps", "256 kbps", "320 kbps"])
        self.ar_var = tk.StringVar(value=KEEP)
        self.ar_combo = combo(5, 2, "音频采样率:", self.ar_var, [KEEP, "44100 Hz", "48000 Hz"])

        self.speed_var = tk.StringVar(value="均衡")
        self.speed_combo = combo(6, 0, "编码速度:", self.speed_var, list(SPEED_LABELS))
        self.hwdec_var = tk.BooleanVar(value=True)
        self.hwdec_chk = ttk.Checkbutton(pf, text="启用硬件解码 (失败会自动关闭重试)", variable=self.hwdec_var)
        self.hwdec_chk.grid(row=6, column=2, columnspan=2, sticky="w")

        self.hwinfo_var = tk.StringVar(value="硬件编码检测: 检测中...")
        self.hwinfo_label = ttk.Label(pf, textvariable=self.hwinfo_var, foreground="#0a7a0a")
        self.hwinfo_label.grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))

        ttk.Separator(pf, orient="horizontal").grid(row=8, column=0, columnspan=4, sticky="ew", pady=8)
        ttk.Label(pf, text="输出文件夹:").grid(row=9, column=0, sticky="w")
        ob = ttk.Frame(pf)
        ob.grid(row=9, column=1, columnspan=3, sticky="ew", padx=(6, 0))
        ob.columnconfigure(0, weight=1)
        self.out_var = tk.StringVar()
        ttk.Entry(ob, textvariable=self.out_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(ob, text="浏览...", width=8, command=self.select_output_dir).grid(row=0, column=1)

        self.fast_disable_widgets = [self.codec_combo, self.hw_combo, self.res_combo,
                                     self.fps_combo, self.abr_combo, self.ar_combo,
                                     self.speed_combo, self.vbr_combo, self.hwdec_chk]

        # ---- 日志（启动时为空）----
        lg = ttk.LabelFrame(right, text=" 3. 执行进度与日志 ", padding=8)
        lg.grid(row=1, column=0, sticky="nsew", padx=(4, 0))
        lg.columnconfigure(0, weight=1)
        lg.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            lg, height=6, state="disabled", font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", relief="flat", padx=6, pady=6)
        self.log_text.grid(row=0, column=0, sticky="nsew")

        # ---- 底部 ----
        cf = ttk.Frame(self.root, padding=(12, 6, 12, 12))
        cf.grid(row=1, column=0, sticky="ew")
        cf.columnconfigure(1, weight=1)

        ttk.Label(cf, text="当前文件:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.file_bar = ttk.Progressbar(cf, maximum=100)
        self.file_bar.grid(row=0, column=1, sticky="ew", padx=(0, 16), ipady=1)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(cf, textvariable=self.status_var).grid(row=0, column=2, sticky="w")

        ttk.Label(cf, text="总体进度:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        self.total_bar = ttk.Progressbar(cf, maximum=100)
        self.total_bar.grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=(6, 0), ipady=2)

        bb = ttk.Frame(cf)
        bb.grid(row=1, column=2, sticky="e", pady=(6, 0))
        self.start_btn = ttk.Button(bb, text="▶ 开始转换", width=14, style="Action.TButton",
                                    command=self.start_conversion)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(bb, text="■ 停止任务", width=12, style="Action.TButton",
                                   command=self.stop_conversion, state="disabled")
        self.stop_btn.pack(side="left")

    def toggle_fast(self):
        fast = self.fast_var.get()
        for w in self.fast_disable_widgets:
            if isinstance(w, ttk.Combobox):
                if fast:
                    w.config(state="disabled")
                else:
                    w.config(state="normal" if w is self.vbr_combo else "readonly")
            else:
                w.config(state="disabled" if fast else "normal")

    def select_all_bitrate(self, event=None):
        """只有当前内容是“原视频”时才全选；已经是具体数字时不全选，方便直接编辑"""
        if self.fast_var.get():
            return

        def _do():
            if self.vbr_var.get().strip() == KEEP:
                self.vbr_combo.selection_range(0, tk.END)

        self.root.after(20, _do)

    # ---------------- 列表操作 ----------------
    def _add_path(self, p: Path):
        if p not in self.files:
            self.files.append(p)
            self.listbox.insert(tk.END, str(p))
            if not self.out_var.get():
                self.out_var.set(str(p.parent / "output"))

    def add_files(self):
        pattern = " ".join("*" + e for e in sorted(SUPPORTED_EXTS))
        fs = filedialog.askopenfilenames(title="选择视频文件",
                                         filetypes=[("视频文件", pattern), ("所有文件", "*.*")])
        for f in fs:
            self._add_path(Path(f))

    def add_folder(self):
        d = filedialog.askdirectory(title="选择文件夹")
        if not d:
            return
        for f in sorted(Path(d).iterdir()):
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS:
                self._add_path(f)

    def remove_selected(self):
        if self.running:
            return
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.files[i]

    def clear_list(self):
        if self.running:
            return
        self.listbox.delete(0, tk.END)
        self.files.clear()

    def select_output_dir(self):
        d = filedialog.askdirectory(title="选择转换后保存的文件夹")
        if d:
            self.out_var.set(d)

    # ---------------- 日志 / 队列 ----------------
    def log(self, msg):
        """线程安全日志"""
        self.q.put(("log", msg))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.log_text.config(state="normal")
                    self.log_text.insert(tk.END, item[1] + "\n")
                    self.log_text.see(tk.END)
                    self.log_text.config(state="disabled")
                elif kind == "hwinfo":
                    self.hwinfo_var.set(item[1])
                    self.hwinfo_label.config(foreground=item[2] if len(item) > 2 else "#0a7a0a")
                elif kind == "prog":
                    _, idx, total, frac, text = item
                    self.file_bar["value"] = frac * 100
                    self.total_bar["value"] = (idx - 1 + frac) / total * 100
                    self.status_var.set(text)
                elif kind == "filedone":
                    _, idx, total = item
                    self.file_bar["value"] = 100
                    self.total_bar["value"] = idx / total * 100
                elif kind == "finished":
                    self._on_finished(*item[1:])
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    # ---------------- 硬件检测 ----------------
    def test_encoder(self, enc):
        """用 0.4 秒测试编码验证编码器是否真的可用，返回 (ok, msg)"""
        with self.enc_lock:
            if enc in self.enc_cache:
                return self.enc_cache[enc]
            cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                   "-i", "color=c=black:s=1280x720:r=25:d=0.4", "-c:v", enc, "-f", "null", "-"]
            rc, out = run_quiet(cmd, timeout=25)
            res = (rc == 0, "" if rc == 0 else last_error_lines(out))
            self.enc_cache[enc] = res
            return res

    def _detect_hw(self):
        rc, _ = run_quiet([self.ffmpeg, "-version"], timeout=15)
        if rc != 0:
            self.q.put(("hwinfo", "❌ 未找到可用的 ffmpeg：请把 ffmpeg.exe 放到程序同目录，或加入系统 PATH", "#c00000"))
            return
        parts = []
        for key, label in (("nvidia", "NVIDIA"), ("amd", "AMD"), ("intel", "Intel")):
            ok, _ = self.test_encoder(ENCODERS[key]["h264"])
            parts.append(f"{label} {'✓' if ok else '✗'}")
        self.q.put(("hwinfo", "硬件编码检测:  " + "   ".join(parts) +
                    "   (✓=可用；全部✗说明驱动或 ffmpeg 版本不支持)"))

    def resolve_backend(self, hw, codec):
        if hw == "cpu":
            return "cpu", ""
        order = ["nvidia", "amd", "intel"] if hw == "auto" else [hw]
        last = ""
        for b in order:
            enc = ENCODERS[b][codec]
            ok, msg = self.test_encoder(enc)
            if ok:
                return b, ""
            last = f"{enc} 不可用: {msg}"
        return "cpu", last

    # ---------------- 控制 ----------------
    def collect_options(self):
        fast = self.fast_var.get()
        ext = self.format_var.get().strip()
        codec = "h265" if self.codec_var.get().startswith("H.265") else "h264"
        hw = HW_LABELS.get(self.hw_var.get(), "auto")

        long_edge = LONG_EDGE.get(self.res_var.get().strip().lower(), 0)

        vb = 0
        raw = self.vbr_var.get().strip()
        if raw and raw != KEEP:
            if not re.fullmatch(r"\d+", raw) or int(raw) <= 0:
                return None
            vb = int(raw)

        def num(s):
            m = re.search(r"\d+", s) if s != KEEP else None
            return m[0] if m else ""

        ab = num(self.abr_var.get())
        return Options(
            fast_copy=fast, ext=ext, codec=codec, hw=hw, hwdec=self.hwdec_var.get(),
            speed=SPEED_LABELS.get(self.speed_var.get(), "balanced"),
            long_edge=long_edge, vbitrate=vb,
            fps="" if self.fps_var.get() == KEEP else self.fps_var.get(),
            abitrate=f"{ab}k" if ab else "", ar=num(self.ar_var.get()),
        )

    def start_conversion(self):
        if self.running:
            return
        if not self.files:
            messagebox.showwarning("提示", "处理列表为空，请先添加视频文件！")
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning("提示", "请设置输出文件夹！")
            return
        opts = self.collect_options()
        if opts is None:
            messagebox.showwarning("码率格式错误", "请在【视频码率】中输入有效的正整数（如 3000）或选择“原视频”！")
            return

        self.running = True
        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.file_bar["value"] = 0
        self.total_bar["value"] = 0
        threading.Thread(target=self._worker, args=(list(self.files), Path(out), opts),
                         daemon=True).start()

    def stop_conversion(self):
        if not self.running:
            return
        self.stop_event.set()
        self._kill_proc()
        self.stop_btn.config(state="disabled")
        self.log(">>> 用户已手动中止转换任务。")

    def _kill_proc(self):
        with self.proc_lock:
            p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("确认退出", "转换仍在进行，确定要退出吗？"):
                return
            self.stop_event.set()
            self._kill_proc()
        self.root.destroy()

    def _on_finished(self, stopped, ok, fail):
        self.running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if stopped:
            self.status_var.set("已停止")
            messagebox.showinfo("已停止", f"转换任务已手动停止。\n已完成: {ok}    失败: {fail}")
        else:
            self.status_var.set("完成")
            messagebox.showinfo("完成", f"批量转换完成！\n成功: {ok}    失败: {fail}")

    # ---------------- 工作线程 ----------------
    def _run_ffmpeg(self, cmd, duration, progress_cb):
        """执行 ffmpeg 并实时解析进度，返回 (returncode, 错误文本)"""
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        with self.proc_lock:
            self.proc = proc
        err = deque(maxlen=40)

        def read_err():
            for line in proc.stderr:
                err.append(line.rstrip())

        t = threading.Thread(target=read_err, daemon=True)
        t.start()

        last_emit = 0.0
        speed = ""
        cur = 0.0
        try:
            for line in proc.stdout:
                key, _, val = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms"):
                    try:
                        cur = int(val) / 1_000_000
                    except ValueError:
                        continue
                elif key == "speed":
                    speed = val.strip()
                elif key == "progress":
                    now = time.time()
                    if now - last_emit > 0.2 or val.strip() == "end":
                        last_emit = now
                        progress_cb(cur, speed)
        finally:
            proc.wait()
            t.join(timeout=2)
            with self.proc_lock:
                self.proc = None
        return proc.returncode, "\n".join(err)

    def _worker(self, files, out_dir, opts):
        ok_cnt = fail_cnt = 0
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log(f"❌ 无法创建输出文件夹: {e}")
            self.q.put(("finished", True, 0, 0))
            return

        total = len(files)
        self.log(f"共 {total} 个文件，开始处理...")

        backend = "cpu"
        if not opts.fast_copy:
            backend, why = self.resolve_backend(opts.hw, opts.codec)
            if opts.hw != "cpu" and backend == "cpu":
                self.log(f"⚠ 显卡编码不可用，已改用 CPU。原因 -> {why}")
            self.log(f"使用编码器: {ENCODERS[backend][opts.codec]}  [{BACKEND_NAME[backend]}]"
                     f"  速度档: {opts.speed}")
        else:
            self.log("极速无损模式：不重新编码视频流")

        used_names = set()
        for idx, src in enumerate(files, start=1):
            if self.stop_event.is_set():
                break

            name = src.stem
            dst = out_dir / f"{name}{opts.ext}"
            try:
                if dst.resolve() == src.resolve():
                    dst = out_dir / f"{name}_converted{opts.ext}"
            except OSError:
                pass
            n = 1
            while str(dst).lower() in used_names:
                dst = out_dir / f"{name}_{n}{opts.ext}"
                n += 1
            used_names.add(str(dst).lower())

            self.log(f"[{idx}/{total}] {src.name}  →  {dst.name}")

            rc, text = run_quiet([self.ffmpeg, "-hide_banner", "-i", str(src)], timeout=40)
            if rc == -1:
                self.log("❌ 找不到 ffmpeg，任务中止。请把 ffmpeg.exe 放到程序同目录。")
                self.stop_event.set()
                break
            info = parse_info(text)
            if not info.has_video:
                self.log("  ✗ 失败：无法识别视频流（文件损坏或格式不受支持）")
                fail_cnt += 1
                self.q.put(("filedone", idx, total))
                continue

            run_opts = opts
            if opts.fast_copy:
                allowed = VIDEO_COPY_OK.get(opts.ext)
                if allowed is not None and info.vcodec not in allowed:
                    self.log(f"  ✗ 失败：视频编码 {info.vcodec} 无法直接封装进 {opts.ext}，"
                             f"请改用 .mkv 或取消“极速无损”")
                    fail_cnt += 1
                    self.q.put(("filedone", idx, total))
                    continue
                run_opts = replace(opts, codec_tag_hvc1=(info.vcodec == "hevc" and opts.ext in (".mp4", ".mov")))

            # 降级链：显卡+硬解 -> 显卡+软解 -> CPU
            if opts.fast_copy or backend == "cpu":
                attempts = [("cpu", False)]
            else:
                attempts = ([(backend, True)] if opts.hwdec else []) + [(backend, False), ("cpu", False)]

            t0 = time.time()
            success = False
            for a_i, (be, hwdec) in enumerate(attempts):
                if self.stop_event.is_set():
                    break
                if a_i > 0:
                    self.log(f"  ↻ 重试: 编码={ENCODERS[be][opts.codec]}, 硬件解码={'开' if hwdec else '关'}")
                cmd = build_cmd(self.ffmpeg, src, dst, run_opts, info, be, hwdec)

                def cb(cur, speed, _idx=idx, _t0=time.time()):
                    if info.duration > 0:
                        frac = min(cur / info.duration, 0.999)
                        el = time.time() - _t0
                        eta = el / frac - el if frac > 0.01 else 0
                        txt = f"{src.name[:28]}  {frac * 100:.0f}%  速度 {speed or '-'}  剩余 {fmt_time(eta)}"
                    else:
                        frac, txt = 0.0, f"{src.name[:28]}  已处理 {fmt_time(cur)}  速度 {speed or '-'}"
                    self.q.put(("prog", _idx, total, frac, txt))

                try:
                    rc, err = self._run_ffmpeg(cmd, info.duration, cb)
                except FileNotFoundError:
                    self.log("❌ 找不到 ffmpeg，任务中止。")
                    self.stop_event.set()
                    break
                except Exception as e:
                    rc, err = -1, str(e)

                if self.stop_event.is_set():
                    break
                if rc == 0 and dst.exists():
                    success = True
                    break
                self.log(f"  ⚠ 本次失败: {last_error_lines(err)}")

            if self.stop_event.is_set():
                try:
                    if dst.exists():
                        dst.unlink()
                except OSError:
                    pass
                break

            if success:
                ok_cnt += 1
                cost = time.time() - t0
                size_mb = dst.stat().st_size / 1024 / 1024
                spd = (info.duration / cost) if (cost > 0 and info.duration) else 0
                self.log(f"  ✓ 成功: {dst.name}  ({size_mb:.1f} MB, 用时 {fmt_time(cost)}"
                         f"{f', 平均 {spd:.1f}x 实时' if spd else ''})")
            else:
                fail_cnt += 1
                self.log(f"  ✗ 失败: {src.name}")
                try:
                    if dst.exists():
                        dst.unlink()
                except OSError:
                    pass
            self.q.put(("filedone", idx, total))

        stopped = self.stop_event.is_set()
        if not stopped:
            self.log(f"=== 全部处理完毕：成功 {ok_cnt}，失败 {fail_cnt} ===")
        self.q.put(("finished", stopped, ok_cnt, fail_cnt))


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    VideoConverterApp(root)
    root.mainloop()
