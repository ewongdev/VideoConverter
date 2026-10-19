import os
import sys
from pathlib import Path
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

# 常量定义
KEEP_RES = "原视频"
KEEP_BITRATE = "原视频"
KEEP_FPS = "原视频"
KEEP_AUDIO_BITRATE = "原视频"
KEEP_AUDIO_AR = "原视频"

# 支持的24种源视频格式后缀
SUPPORTED_EXTS = {
    ".mpg", ".mpeg", ".avi", ".mov", ".wmv", ".flv", ".f4v", ".mkv",
    ".mp4", ".m4v", ".rm", ".rmvb", ".vob", ".ts", ".m2ts", ".mts",
    ".webm", ".3gp", ".3g2", ".ogv", ".dv", ".divx", ".asf", ".mxf"
}

def get_ffmpeg_path():
    """获取 ffmpeg.exe 的绝对路径（兼容 PyInstaller 单文件打包模式）"""
    if getattr(sys, 'frozen', False):
        # 运行在打包后的 exe 临时环境中
        base_path = sys._MEIPASS
    else:
        # 运行在普通 Python 开发环境中
        base_path = os.path.dirname(os.path.abspath(__file__))
    
    ffmpeg_exe = os.path.join(base_path, "ffmpeg.exe")
    # 如果找到了内嵌的 ffmpeg.exe 就用它，否则尝试调用系统环境变量
    return ffmpeg_exe if os.path.exists(ffmpeg_exe) else "ffmpeg"


class VideoConverterApp:

  def __init__(self, root):
    self.root = root
    self.root.title("视频格式转换工具")
    
    # --- 窗口尺寸与屏幕居中自适应优化 ---
    screen_width = self.root.winfo_screenwidth()
    screen_height = self.root.winfo_screenheight()
    win_width = min(1280, screen_width - 60)   # ⬅️ 修改这里：默认窗口宽度
    win_height = min(720, screen_height - 80)  # ⬅️ 修改这里：默认窗口高度
    x = max(0, (screen_width - win_width) // 2)
    y = max(0, (screen_height - win_height) // 2 - 20)
    self.root.geometry(f"{win_width}x{win_height}+{x}+{y}")
    self.root.minsize(950, 520)                # ⬅️ 修改这里：窗口允许缩小的最小极限尺寸 (宽x高)
    self.root.resizable(True, True)

    self.is_running = False
    self.current_process = None
    self.file_list_data = []
    
    # 初始化内嵌 FFmpeg 路径
    self.ffmpeg_path = get_ffmpeg_path()

    self.setup_ui()

  def setup_ui(self):
    # ================= 0. 全局样式美化 =================
    style = ttk.Style()
    if "vista" in style.theme_names():
      style.theme_use("vista")
    elif "clam" in style.theme_names():
      style.theme_use("clam")

    default_font = ("Microsoft YaHei UI", 9)
    bold_font = ("Microsoft YaHei UI", 9, "bold")

    style.configure(".", font=default_font)
    style.configure("TLabelframe.Label", font=bold_font, foreground="#1f2937")
    style.configure("TButton", padding=(6, 3))
    style.configure("Action.TButton", font=bold_font, padding=(10, 5))
    style.configure("Fast.TCheckbutton", font=("Microsoft YaHei UI", 9, "bold"), foreground="#0066cc")

    # 根窗口网格权重配置
    self.root.columnconfigure(0, weight=1)
    self.root.rowconfigure(0, weight=1)

    # 主工作区容器：使用 tk.PanedWindow 实现中间左右拖拽调节宽度
    main_paned = tk.PanedWindow(
        self.root,
        orient=tk.HORIZONTAL,
        sashwidth=6,             # 中间拖拽分隔条的宽度
        sashrelief=tk.RAISED,    # 分隔条带微凸质感，方便识别拖拽位置
        bg="#dcdcdc",            # 分隔条颜色
        bd=0,
        opaqueresize=True        # 拖拽时实时平滑刷新内容大小
    )
    main_paned.grid(row=0, column=0, sticky="nsew", padx=12, pady=(10, 4))

    # ================= 1. 左侧：待处理的视频文件区域 =================
    left_pane = ttk.Frame(main_paned)
    left_pane.columnconfigure(0, weight=1)
    left_pane.rowconfigure(0, weight=1)

    file_frame = ttk.LabelFrame(
        left_pane, 
        text=" 1. 待处理的视频文件", 
        padding=10
    )
    file_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
    file_frame.columnconfigure(0, weight=1)
    file_frame.rowconfigure(1, weight=1)

    # 顶部操作按钮栏（4列均分自适应）
    btn_bar = ttk.Frame(file_frame)
    btn_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    for i in range(4):
      btn_bar.columnconfigure(i, weight=1)

    ttk.Button(btn_bar, text="➕ 添加视频文件", command=self.add_files).grid(row=0, column=0, sticky="ew", padx=(0, 4))
    ttk.Button(btn_bar, text="📁 添加文件夹", command=self.add_folder).grid(row=0, column=1, sticky="ew", padx=4)
    ttk.Button(btn_bar, text="➖ 移除选中", command=self.remove_selected).grid(row=0, column=2, sticky="ew", padx=4)
    ttk.Button(btn_bar, text="🗑 清空列表", command=self.clear_list).grid(row=0, column=3, sticky="ew", padx=(4, 0))

    # 视频列表框（占满左侧剩余全部高度）
    list_frame = ttk.Frame(file_frame)
    list_frame.grid(row=1, column=0, sticky="nsew")
    list_frame.columnconfigure(0, weight=1)
    list_frame.rowconfigure(0, weight=1)
    
    self.listbox = tk.Listbox(
        list_frame, 
        selectmode=tk.EXTENDED,
        font=("Microsoft YaHei UI", 9),
        relief="solid",
        bd=1,
        highlightthickness=0,
        selectbackground="#0078D7",
        selectforeground="white",
        activestyle="none"
    )
    self.listbox.grid(row=0, column=0, sticky="nsew")
    
    scrollbar_y = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
    scrollbar_y.grid(row=0, column=1, sticky="ns")
    scrollbar_x = ttk.Scrollbar(list_frame, orient="horizontal", command=self.listbox.xview)
    scrollbar_x.grid(row=1, column=0, sticky="ew")
    self.listbox.config(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

    # ================= 右侧容器（上：参数设置+输出目录，下：执行日志） =================
    right_container = ttk.Frame(main_paned)
    right_container.columnconfigure(0, weight=1)
    right_container.rowconfigure(1, weight=1)  # 让下半部分的日志框自动填充剩余高度

    # 将左右两个面板加入可拖拽容器，并设置初始宽度与最小防折叠宽度
    main_paned.add(left_pane, width=700, minsize=320, stretch="always")  # ⬅️ 修改左侧宽度
    main_paned.add(right_container, minsize=500, stretch="always")       # ⬅️ 修改右侧宽度

    # ================= 2. 右上：转换参数配置区域 =================
    param_frame = ttk.LabelFrame(right_container, text=" 2. 参数设置 ", padding=(12, 8, 12, 10))
    param_frame.grid(row=0, column=0, sticky="ew", padx=(4, 0), pady=(0, 8))
    param_frame.columnconfigure(1, weight=1)
    param_frame.columnconfigure(3, weight=1)

    top_bar = ttk.Frame(param_frame)
    top_bar.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
    self.fast_mode_var = tk.BooleanVar(value=False)
    self.fast_chk = ttk.Checkbutton(
        top_bar, text="⚡ 极速无损模式 (仅转换视频格式，不修改视频参数)",
        variable=self.fast_mode_var, command=self.toggle_fast_mode,
        style="Fast.TCheckbutton"
    )
    self.fast_chk.pack(side="left")

    ttk.Separator(param_frame, orient="horizontal").grid(row=1, column=0, columnspan=4, sticky="ew", pady=(2, 8))

    # 右侧下拉框宽度
    left_combo_w = 24
    right_combo_w = 42

    # 第一行参数
    ttk.Label(param_frame, text="目标文件格式:").grid(row=2, column=0, sticky="w", pady=5)
    self.format_var = tk.StringVar(value=".mp4")
    self.format_combo = ttk.Combobox(
        param_frame, textvariable=self.format_var, values=[".mp4", ".mkv", ".mov"],
        state="readonly", width=left_combo_w
    )
    self.format_combo.grid(row=2, column=1, sticky="ew", padx=(6, 14), pady=5)

    ttk.Label(param_frame, text="视频压缩编码:").grid(row=2, column=2, sticky="w", pady=5)
    self.codec_std_var = tk.StringVar(value="H.264 (兼容性最好)")
    self.codec_std_combo = ttk.Combobox(
        param_frame, textvariable=self.codec_std_var,
        values=["H.264 (兼容性最好)", "H.265 (压缩率高/兼容性差)"],
        state="readonly", width=right_combo_w
    )
    self.codec_std_combo.grid(row=2, column=3, sticky="ew", padx=(6, 0), pady=5)

    # 第二行参数
    ttk.Label(param_frame, text="硬件加速方式:").grid(row=3, column=0, sticky="w", pady=5)
    self.hw_var = tk.StringVar(value="NVIDIA显卡")
    self.hw_combo = ttk.Combobox(
        param_frame, textvariable=self.hw_var,
        values=["NVIDIA显卡", "AMD显卡", "CPU (通用稳定)"],
        state="readonly", width=left_combo_w
    )
    self.hw_combo.grid(row=3, column=1, sticky="ew", padx=(6, 14), pady=5)

    ttk.Label(param_frame, text="画面尺寸:").grid(row=3, column=2, sticky="w", pady=5)
    self.res_var = tk.StringVar(value=KEEP_RES)
    self.res_combo = ttk.Combobox(
        param_frame, textvariable=self.res_var,
        values=[
            KEEP_RES, 
            "480p (横854×480 | 竖480×854)", 
            "720p (横1280×720 | 竖720×1280)", 
            "1080p (横1920×1080 | 竖1080×1920)", 
            "2k (横2560×1440 | 竖1440×2560)", 
            "4k (横3840×2160 | 竖2160×3840)"
        ],
        state="readonly", width=right_combo_w
    )
    self.res_combo.grid(row=3, column=3, sticky="ew", padx=(6, 0), pady=5)

    # 第三行参数
    ttk.Label(param_frame, text="视频码率(kbps):").grid(row=4, column=0, sticky="w", pady=5)
    self.vbitrate_var = tk.StringVar(value=KEEP_BITRATE)
    self.vbitrate_combo = ttk.Combobox(
        param_frame, textvariable=self.vbitrate_var,
        values=[KEEP_BITRATE, "1500", "2000", "3000", "4000", "5000", "10000"], width=left_combo_w
    )
    self.vbitrate_combo.grid(row=4, column=1, sticky="ew", padx=(6, 14), pady=5)
    self.vbitrate_combo.bind("<Button-1>", self.select_all_bitrate)
    self.vbitrate_combo.bind("<FocusIn>", self.select_all_bitrate)

    ttk.Label(param_frame, text="画面帧率(FPS):").grid(row=4, column=2, sticky="w", pady=5)
    self.fps_var = tk.StringVar(value=KEEP_FPS)
    self.fps_combo = ttk.Combobox(
        param_frame, textvariable=self.fps_var,
        values=[KEEP_FPS, "25", "30", "60"],
        state="readonly", width=right_combo_w
    )
    self.fps_combo.grid(row=4, column=3, sticky="ew", padx=(6, 0), pady=5)

    # 第四行参数
    ttk.Label(param_frame, text="音频质量(码率):").grid(row=5, column=0, sticky="w", pady=5)
    self.abitrate_var = tk.StringVar(value=KEEP_AUDIO_BITRATE)
    self.abitrate_combo = ttk.Combobox(
        param_frame, textvariable=self.abitrate_var,
        values=[KEEP_AUDIO_BITRATE, "128 kbps", "192 kbps", "256 kbps", "320 kbps"],
        state="readonly", width=left_combo_w
    )
    self.abitrate_combo.grid(row=5, column=1, sticky="ew", padx=(6, 14), pady=5)

    ttk.Label(param_frame, text="音频采样率:").grid(row=5, column=2, sticky="w", pady=5)
    self.ar_var = tk.StringVar(value=KEEP_AUDIO_AR)
    self.ar_combo = ttk.Combobox(
        param_frame, textvariable=self.ar_var,
        values=[KEEP_AUDIO_AR, "44100 Hz", "48000 Hz"],
        state="readonly", width=right_combo_w
    )
    self.ar_combo.grid(row=5, column=3, sticky="ew", padx=(6, 0), pady=5)

    # 分割线与输出目录（位于右侧参数框底部）
    ttk.Separator(param_frame, orient="horizontal").grid(row=6, column=0, columnspan=4, sticky="ew", pady=(8, 8))

    ttk.Label(param_frame, text="输出文件夹:").grid(row=7, column=0, sticky="w", pady=2)
    out_box = ttk.Frame(param_frame)
    out_box.grid(row=7, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=2)
    out_box.columnconfigure(0, weight=1)

    self.output_dir_var = tk.StringVar()
    ttk.Entry(out_box, textvariable=self.output_dir_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
    ttk.Button(out_box, text="浏览...", width=8, command=self.select_output_dir).grid(row=0, column=1, sticky="e")

    # ================= 3. 右下：执行日志输出区域 =================
    log_frame = ttk.LabelFrame(right_container, text=" 3. 执行进度与日志 ", padding=8)
    log_frame.grid(row=1, column=0, sticky="nsew", padx=(4, 0))
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)

    self.log_text = scrolledtext.ScrolledText(
        log_frame, 
        height=6, 
        state="disabled", 
        font=("Consolas", 9),
        bg="#1e1e1e",          # 深色护眼终端背景
        fg="#d4d4d4",          # 柔和浅灰字体
        insertbackground="white",
        relief="flat",
        padx=6,
        pady=6
    )
    self.log_text.grid(row=0, column=0, sticky="nsew")

    # ================= 4. 底部通栏：进度条与控制按钮区域 =================
    ctrl_frame = ttk.Frame(self.root, padding=(12, 6, 12, 12))
    ctrl_frame.grid(row=1, column=0, sticky="ew")
    ctrl_frame.columnconfigure(1, weight=1)

    ttk.Label(ctrl_frame, text="总体进度:").grid(row=0, column=0, sticky="w", padx=(0, 8))

    self.progress_var = tk.DoubleVar(value=0)
    self.progress_bar = ttk.Progressbar(ctrl_frame, variable=self.progress_var, maximum=100)
    self.progress_bar.grid(row=0, column=1, sticky="ew", padx=(0, 16), ipady=2)

    btn_box = ttk.Frame(ctrl_frame)
    btn_box.grid(row=0, column=2, sticky="e")

    self.start_btn = ttk.Button(
        btn_box, text="▶ 开始转换", command=self.start_conversion, width=15, style="Action.TButton"
    )
    self.start_btn.pack(side="left", padx=(0, 8))

    self.stop_btn = ttk.Button(
        btn_box, text="■ 停止任务", command=self.stop_conversion, state="disabled", width=12, style="Action.TButton"
    )
    self.stop_btn.pack(side="left")

  # --- 交互联动与控制 ---
  def toggle_fast_mode(self):
    if self.fast_mode_var.get():
      combo_state = "disabled"
      entry_state = "disabled"
    else:
      combo_state = "readonly"
      entry_state = "normal"
      
    self.codec_std_combo.config(state=combo_state)
    self.hw_combo.config(state=combo_state)
    self.res_combo.config(state=combo_state)
    self.fps_combo.config(state=combo_state)
    self.abitrate_combo.config(state=combo_state)
    self.ar_combo.config(state=combo_state)
    self.vbitrate_combo.config(state=entry_state)

  # --- 列表操作方法 ---
  def add_files(self):
    files = filedialog.askopenfilenames(
        title="选择视频文件",
        filetypes=[("视频文件", "*" + " *".join(SUPPORTED_EXTS)), ("所有文件", "*.*")]
    )
    for f in files:
      path_obj = Path(f)
      if path_obj not in self.file_list_data:
        self.file_list_data.append(path_obj)
        self.listbox.insert(tk.END, str(path_obj))
        if not self.output_dir_var.get():
          self.output_dir_var.set(str(path_obj.parent / "output"))

  def add_folder(self):
    folder = filedialog.askdirectory(title="选择文件夹")
    if folder:
      folder_path = Path(folder)
      found_any = False
      for f in folder_path.iterdir():
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS:
          if f not in self.file_list_data:
            self.file_list_data.append(f)
            self.listbox.insert(tk.END, str(f))
            found_any = True
      
      if found_any and not self.output_dir_var.get():
        self.output_dir_var.set(str(folder_path / "output"))

  def remove_selected(self):
    selected_indices = self.listbox.curselection()
    for index in reversed(selected_indices):
      self.listbox.delete(index)
      del self.file_list_data[index]

  def clear_list(self):
    self.listbox.delete(0, tk.END)
    self.file_list_data.clear()

  def select_output_dir(self):
    folder = filedialog.askdirectory(title="选择转换后保存的文件夹")
    if folder:
      self.output_dir_var.set(folder)

  def select_all_bitrate(self, event=None):
    if not self.fast_mode_var.get():
      self.root.after(20, lambda: self.vbitrate_combo.selection_range(0, tk.END))

  def log(self, message):
    self.log_text.config(state="normal")
    self.log_text.insert(tk.END, message + "\n")
    self.log_text.see(tk.END)
    self.log_text.config(state="disabled")

  # --- FFmpeg 参数与执行 ---
  def get_video_encoder(self, hw_choice, std_choice):
    is_h265 = "h265" in std_choice.lower()
    if "N卡" in hw_choice:
      return "hevc_nvenc" if is_h265 else "h264_nvenc"
    elif "A卡" in hw_choice:
      return "hevc_amf" if is_h265 else "h264_amf"
    else:
      return "libx265" if is_h265 else "libx264"

  def parse_bitrate(self, raw_val):
    val = raw_val.strip()
    if val == KEEP_BITRATE or not val: return None
    match = re.search(r"(\d+)", val)
    return f"{match.group(1)}k" if match else None

  def probe_source_bitrate(self, input_path):
    try:
      proc = subprocess.Popen(
          [self.ffmpeg_path, "-i", str(input_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
          universal_newlines=True, encoding="utf-8", errors="ignore",
          creationflags=subprocess.CREATE_NO_WINDOW
      )
      _, stderr = proc.communicate()
      match = re.search(r"bitrate:\s*(\d+)\s*kb/s", stderr)
      if match: return f"{match.group(1)}k"
    except Exception: pass
    return None

  def build_ffmpeg_cmd(self, input_path, output_path, force_reencode_audio=False):
    cmd = [self.ffmpeg_path, "-i", str(input_path)]

    if self.fast_mode_var.get():
      cmd.extend(["-c", "copy", "-y", str(output_path)])
      return cmd

    hw_choice, std_choice = self.hw_var.get(), self.codec_std_var.get()
    res_choice, fps_choice = self.res_var.get(), self.fps_var.get()
    custom_bitrate = self.parse_bitrate(self.vbitrate_var.get())
    abit_choice = self.abitrate_var.get()
    ar_choice = self.ar_var.get()

    encoder = self.get_video_encoder(hw_choice, std_choice)
    cmd.extend(["-c:v", encoder])

    if "265" in encoder or "hevc" in encoder:
      if output_path.suffix.lower() in {".mp4", ".mov"}:
        cmd.extend(["-tag:v", "hvc1"])

    if custom_bitrate:
      cmd.extend(["-b:v", custom_bitrate])
    else:
      src_bitrate = self.probe_source_bitrate(input_path)
      if src_bitrate: cmd.extend(["-b:v", src_bitrate])
      elif encoder == "libx264": cmd.extend(["-crf", "18"])
      elif encoder == "libx265": cmd.extend(["-crf", "22"])
      else: cmd.extend(["-b:v", "5000k"])

    if res_choice != KEEP_RES:
      long_edge_map = {"480p": "854", "720p": "1280", "1080p": "1920", "2k": "2560", "4k": "3840"}
      for key, edge_val in long_edge_map.items():
        if res_choice.lower().startswith(key):
          scale_expr = f"scale=if(gt(iw\\,ih)\\,{edge_val}\\,-2):if(gt(iw\\,ih)\\,-2\\,{edge_val})"
          cmd.extend(["-vf", scale_expr])
          break

    if fps_choice != KEEP_FPS: cmd.extend(["-r", fps_choice])

    if abit_choice == KEEP_AUDIO_BITRATE and ar_choice == KEEP_AUDIO_AR and not force_reencode_audio:
      cmd.extend(["-c:a", "copy"])
    else:
      cmd.extend(["-c:a", "aac"])
      if abit_choice != KEEP_AUDIO_BITRATE: cmd.extend(["-b:a", abit_choice])
      else: cmd.extend(["-b:a", "192k"]) 
      if ar_choice != KEEP_AUDIO_AR: cmd.extend(["-ar", ar_choice])

    cmd.extend(["-y", str(output_path)])
    return cmd

  def start_conversion(self):
    if not self.file_list_data:
      messagebox.showwarning("提示", "处理列表为空，请先添加视频文件！")
      return

    out_dir_str = self.output_dir_var.get().strip()
    if not out_dir_str:
      messagebox.showwarning("提示", "请设置输出文件夹！")
      return
      
    if not self.fast_mode_var.get():
      raw_bit = self.vbitrate_var.get().strip()
      if raw_bit != KEEP_BITRATE and self.parse_bitrate(raw_bit) is None:
        messagebox.showwarning("码率格式错误", "请在【视频码率】中输入有效的数字（如 3000）！")
        return

    self.is_running = True
    self.start_btn.config(state="disabled")
    self.stop_btn.config(state="normal")
    self.progress_var.set(0)

    threading.Thread(target=self.run_batch, args=(Path(out_dir_str),), daemon=True).start()

  def stop_conversion(self):
    if self.is_running:
      self.is_running = False
      if self.current_process: self.current_process.terminate()
      self.log(">>> 用户已手动中止转换任务。")

  def run_ffmpeg_process(self, cmd):
    self.current_process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        encoding="utf-8", errors="ignore", creationflags=subprocess.CREATE_NO_WINDOW
    )
    _, stderr = self.current_process.communicate()
    return self.current_process.returncode, stderr

  def run_batch(self, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    total = len(self.file_list_data)
    out_ext = self.format_var.get().strip()
    
    self.log(f"共加载 {total} 个视频文件，准备开始处理...")

    for idx, file_path in enumerate(self.file_list_data, start=1):
      if not self.is_running: break

      out_file = out_dir / f"{file_path.stem}{out_ext}"
      if out_file.resolve() == file_path.resolve():
        out_file = out_dir / f"{file_path.stem}_converted{out_ext}"

      self.log(f"[{idx}/{total}] 正在处理: {file_path.name} → {out_file.name}")
      cmd = self.build_ffmpeg_cmd(file_path, out_file, force_reencode_audio=False)

      try:
        returncode, _ = self.run_ffmpeg_process(cmd)
        
        if returncode != 0 and self.is_running:
          if self.fast_mode_var.get():
             self.log(f"  ✗ 失败: {file_path.name} (原编码不支持塞入目标格式，请取消勾选极速无损)")
          elif "copy" in cmd:
            self.log(f"  ↻ 提示: 原音频格式不兼容目标容器，正自动转换为通用 AAC 音频...")
            fallback_cmd = self.build_ffmpeg_cmd(file_path, out_file, force_reencode_audio=True)
            returncode, _ = self.run_ffmpeg_process(fallback_cmd)
            
            if returncode == 0: self.log(f"  ✓ 成功: {out_file.name}")
            else: self.log(f"  ✗ 失败: {file_path.name} (硬件加速异常，建议尝试切为 CPU 加速)")
          else:
            self.log(f"  ✗ 失败: {file_path.name} (硬件加速异常，建议尝试切为 CPU 加速)")
            
        elif returncode == 0:
          self.log(f"  ✓ 成功: {out_file.name}")
          
      except Exception as e:
        self.log(f"执行异常（未找到 ffmpeg 或权限被拒绝）: {e}")
        break

      self.progress_var.set((idx / total) * 100)

    if self.is_running:
      self.log("=== 所有视频批量处理完毕！ ===")
      messagebox.showinfo("完成", "批量视频转换已完成！")

    self.reset_buttons()

  def reset_buttons(self):
    self.is_running = False
    self.current_process = None
    self.start_btn.config(state="normal")
    self.stop_btn.config(state="disabled")


if __name__ == "__main__":
  # 开启 Windows 高分屏 DPI 自适应，防止界面字体模糊或比例失调
  if sys.platform == "win32":
    try:
      import ctypes
      ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
      pass

  root = tk.Tk()
  app = VideoConverterApp(root)
  root.mainloop()
