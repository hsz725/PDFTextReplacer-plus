# -*- coding: utf-8 -*-
"""
PDF 工具图形界面

两个功能页：
  1. 文本替换 —— 查找并替换 PDF 中的文字（保留原字体风格）
  2. 区域擦除 —— 按"左上角坐标 + 宽高"擦掉指定矩形内的所有内容（不限文字）

坐标系说明：界面上一律使用**左上角原点**（X 向右、Y 向下），与 Acrobat/福昕
等阅读器的测量工具一致；PDF 文件内部是左下角原点、Y 向上，转换在 pdf_eraser 里做。
"""

import os
import sys
import threading
import logging
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pymupdf

from pdf_replacer_pymupdf import PyMuPDFTextReplacer, verify_replacements
from pdf_eraser import (EraseTask, detect_background_color, erase_document,
                        hex_to_rgb, parse_page_number, rgb_to_hex,
                        ui_rect_to_pdf_rect, validate_rect)

PT_PER_MM = 72.0 / 25.4

# 精简版开关：True 时不创建"区域擦除"标签页，界面只保留文本替换。
# 由入口脚本（tools/entry_lite.py）在创建窗口前设置，打包时通过不同入口区分版本。
# 注意：必须在 App() 实例化之前赋值才生效。
LITE_MODE = False


def font_dirs() -> list:
    """返回字体搜索目录列表（按优先级）。

    exe 同级目录优先，程序内置目录兜底——两者**同时**参与查找。
    这样用户即使在 exe 旁边建了个 fonts 目录但没放字体，
    也不会把程序自带的字体屏蔽掉。
    """
    dirs = []
    if getattr(sys, 'frozen', False):
        beside_exe = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "fonts")
        if os.path.isdir(beside_exe):
            dirs.append(beside_exe)
    bundled = os.path.join(getattr(sys, '_MEIPASS', os.path.abspath(".")), "fonts")
    if os.path.isdir(bundled) and bundled not in dirs:
        dirs.append(bundled)
    return dirs


def resource_path(relative_path: str) -> str:
    """获取随程序分发的资源路径（主要是 fonts 目录）。

    打包后优先用 exe 同级目录，这样用户能自己往 exe 旁边放中文字体；
    找不到才退回 PyInstaller 的临时解包目录（_MEIPASS）。
    """
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidate = os.path.join(exe_dir, relative_path)
        if os.path.exists(candidate):
            return candidate
    base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
    return os.path.join(base_path, relative_path)


# --- GUI Logger ---
class TextHandler(logging.Handler):
    """自定义日志处理器，将日志记录重定向到Tkinter的Text小部件"""
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state='normal')
            self.text_widget.insert(tk.END, msg + '\n')
            self.text_widget.configure(state='disabled')
            self.text_widget.yview(tk.END)
        self.text_widget.after(0, append)


# --- 主应用 ---
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF 文本替换工具" if LITE_MODE else "PDF 工具箱（文本替换 / 区域擦除）")
        self.geometry("820x800")  # 稍后按内容自动调整

        # --- 变量 ---
        self.input_pdf_path = tk.StringVar()
        self.output_pdf_path = tk.StringVar()

        # 文本替换
        self.method = tk.StringVar(value='precise')
        self.verify = tk.BooleanVar(value=True)
        self.font_scope = tk.StringVar(value="open")   # open=仅开源 / all=含系统字体
        self.custom_font = None                        # 用户临时指定的字体文件

        # 区域擦除（Lite 版不建这些控件，但变量先建好，避免回调里取不到）
        self.page_no = tk.StringVar(value="1")
        self.unit = tk.StringVar(value="pt")          # pt / mm
        self.rect_x = tk.StringVar(value="0")
        self.rect_y = tk.StringVar(value="0")
        self.rect_w = tk.StringVar(value="100")
        self.rect_h = tk.StringVar(value="30")
        self.bg_hex = tk.StringVar(value="#FFFFFF")

        self._page_total = 0
        self._page_rect = None          # 当前页 pymupdf.Rect（单位 pt）
        self._auto_bg = (1.0, 1.0, 1.0)

        # --- UI 布局 ---
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        file_frame = ttk.LabelFrame(main_frame, text="文件路径", padding="10")
        file_frame.pack(fill=tk.X, pady=5)
        self._create_file_widgets(file_frame)

        if LITE_MODE:
            # 精简版：只有一个功能，不套标签页，界面更直接
            self.notebook = None
            text_tab = ttk.Frame(main_frame)
            text_tab.pack(fill=tk.BOTH, expand=True, pady=5)
            self._create_text_tab(text_tab)
        else:
            self.notebook = ttk.Notebook(main_frame)
            self.notebook.pack(fill=tk.BOTH, expand=True, pady=5)
            self._create_text_tab(self.notebook)
            self._create_erase_tab(self.notebook)

        run_button = ttk.Button(main_frame, text="开始处理", command=self.start_processing_thread)
        run_button.pack(pady=8, fill=tk.X)

        log_frame = ttk.LabelFrame(main_frame, text="日志", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self._create_log_widget(log_frame)

        self.font_scope.trace_add("write", lambda *_: self._update_font_scope_hint())

        if not LITE_MODE:
            # 页码 / 单位变化时刷新页面信息
            self.page_no.trace_add("write", lambda *_: self._update_page_display())
            self.unit.trace_add("write", lambda *_: self._update_page_display())

        self._fit_window()

    def _fit_window(self):
        """按内容实际需要的尺寸定窗口高度，避免底部控件被裁掉。

        注意要逐个切换标签页测量——两个页的高度不一样，只量当前页会让
        另一个页的底部被裁掉。
        """
        self.update_idletasks()
        need_h = self.winfo_reqheight()
        if self.notebook is not None:
            current = self.notebook.select()
            for tab in self.notebook.tabs():
                self.notebook.select(tab)
                self.update_idletasks()
                need_h = max(need_h, self.winfo_reqheight())
            self.notebook.select(current)
            self.update_idletasks()

        need_w = max(820, self.winfo_reqwidth())
        # 预留一点余量：ttk 的 reqheight 对嵌套 LabelFrame 会略微低估
        max_h = int(self.winfo_screenheight() * 0.92)
        self.geometry(f"{need_w}x{min(max(need_h + 40, 700), max_h)}")

    # ---------------- 文件路径 ----------------
    def _create_file_widgets(self, parent):
        ttk.Label(parent, text="输入PDF:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=3)
        ttk.Entry(parent, textvariable=self.input_pdf_path, width=60).grid(row=0, column=1, sticky=tk.EW)
        ttk.Button(parent, text="浏览...", command=self.browse_input_pdf).grid(row=0, column=2, padx=5)

        ttk.Label(parent, text="输出PDF:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=3)
        ttk.Entry(parent, textvariable=self.output_pdf_path, width=60).grid(row=1, column=1, sticky=tk.EW)
        ttk.Button(parent, text="选择...", command=self.browse_output_pdf).grid(row=1, column=2, padx=5)

        parent.columnconfigure(1, weight=1)

    # ---------------- 文本替换页 ----------------
    def _create_text_tab(self, parent):
        """parent 是 Notebook 时作为标签页加入；否则直接铺在容器里（精简版）"""
        if isinstance(parent, ttk.Notebook):
            tab = ttk.Frame(parent)
            parent.add(tab, text="文本替换")
        else:
            tab = parent

        rules_frame = ttk.LabelFrame(tab, text="替换规则 (格式: 原文|译文)", padding="10")
        rules_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self._create_rules_editor(rules_frame)

        options_frame = ttk.LabelFrame(tab, text="选项", padding="10")
        options_frame.pack(fill=tk.X, pady=5)
        self._create_options_widgets(options_frame)

    def _create_rules_editor(self, parent):
        self.rules_text = tk.Text(parent, wrap=tk.WORD, height=8)
        self.rules_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rules_scrollbar = ttk.Scrollbar(parent, command=self.rules_text.yview)
        rules_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.rules_text['yscrollcommand'] = rules_scrollbar.set
        self.rules_text.insert(tk.END, """# 在这里输入规则, 每行一条, 例如:
# old text|new text
""")

    def _create_options_widgets(self, parent):
        # 第一行：替换方法 + 验证
        ttk.Label(parent, text="替换方法:").grid(row=0, column=0, sticky=tk.W, padx=(2, 4), pady=3)
        methods = ['precise', 'overlay', 'hybrid']
        ttk.OptionMenu(parent, self.method, self.method.get(), *methods).grid(
            row=0, column=1, sticky=tk.W)
        ttk.Checkbutton(parent, text="处理后验证结果", variable=self.verify).grid(
            row=0, column=3, sticky=tk.E, padx=(7, 10))

        # 第二行：字体范围（默认仅开源，保证产出可直接分发）
        ttk.Label(parent, text="字体范围:").grid(row=1, column=0, sticky=tk.W, padx=(2, 4), pady=3)
        scope = ttk.Frame(parent)
        scope.grid(row=1, column=1, columnspan=2, sticky=tk.W)
        ttk.Radiobutton(scope, text="仅使用开源字体", value="open",
                        variable=self.font_scope).pack(side=tk.LEFT)
        ttk.Radiobutton(scope, text="使用所有字体（含系统字体）", value="all",
                        variable=self.font_scope).pack(side=tk.LEFT, padx=(12, 0))
        self.font_scope_hint = ttk.Label(parent, text="", foreground="#888888")
        self.font_scope_hint.grid(row=1, column=3, sticky=tk.E, padx=(7, 10))

        # 第三行：临时指定字体文件
        ttk.Label(parent, text="自定义字体:").grid(row=2, column=0, sticky=tk.W, padx=(2, 4), pady=3)
        font_row = ttk.Frame(parent)
        font_row.grid(row=2, column=1, columnspan=3, sticky=tk.W)
        self.font_file_label = ttk.Label(font_row, text="未指定", foreground="#888888")
        self.font_file_label.pack(side=tk.LEFT)
        ttk.Button(font_row, text="选择字体文件…", command=self.pick_font_file).pack(
            side=tk.LEFT, padx=(10, 4))
        ttk.Button(font_row, text="清除", command=self.clear_font_file).pack(side=tk.LEFT)

        parent.columnconfigure(1, weight=1)
        self._update_font_scope_hint()

    # ---------------- 区域擦除页 ----------------
    def _create_erase_tab(self, parent):
        tab = ttk.Frame(parent)
        parent.add(tab, text="区域擦除")

        # 页码
        page_frame = ttk.LabelFrame(tab, text="页面", padding=8)
        page_frame.pack(fill=tk.X, pady=4)

        ttk.Label(page_frame, text="第").pack(side=tk.LEFT)
        self.page_spin = ttk.Spinbox(page_frame, from_=1, to=1, width=6,
                                     textvariable=self.page_no)
        self.page_spin.pack(side=tk.LEFT, padx=4)
        self.page_total_label = ttk.Label(page_frame, text="页，共 0 页")
        self.page_total_label.pack(side=tk.LEFT)

        ttk.Button(page_frame, text="刷新页面信息", command=self.load_pdf_info).pack(side=tk.RIGHT)

        # 矩形 + 底色 + 页面信息，合并成一个区块，省纵向空间
        box = ttk.LabelFrame(tab, text="擦除矩形（按左上角定位）", padding=8)
        box.pack(fill=tk.X, pady=4)
        box.columnconfigure(5, weight=1)

        ttk.Label(box, text="左上角 X:").grid(row=0, column=0, sticky=tk.W, padx=(2, 4), pady=3)
        ttk.Entry(box, textvariable=self.rect_x, width=9).grid(row=0, column=1, padx=2)
        ttk.Label(box, text="Y:").grid(row=0, column=2, sticky=tk.W, padx=(8, 4))
        ttk.Entry(box, textvariable=self.rect_y, width=9).grid(row=0, column=3, padx=2)

        ttk.Label(box, text="宽:").grid(row=1, column=0, sticky=tk.W, padx=(2, 4), pady=3)
        ttk.Entry(box, textvariable=self.rect_w, width=9).grid(row=1, column=1, padx=2)
        ttk.Label(box, text="高:").grid(row=1, column=2, sticky=tk.W, padx=(8, 4))
        ttk.Entry(box, textvariable=self.rect_h, width=9).grid(row=1, column=3, padx=2)

        unit_frame = ttk.Frame(box)
        unit_frame.grid(row=0, column=4, rowspan=2, padx=(14, 4))
        ttk.Label(unit_frame, text="单位").pack(anchor=tk.W)
        radio_row = ttk.Frame(unit_frame)
        radio_row.pack(anchor=tk.W)
        ttk.Radiobutton(radio_row, text="pt", value="pt", variable=self.unit).pack(side=tk.LEFT)
        ttk.Radiobutton(radio_row, text="mm", value="mm", variable=self.unit).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Button(box, text="预览矩形", command=self.preview_rect).grid(
            row=0, column=6, rowspan=2, padx=(6, 2))

        # 底色
        ttk.Label(box, text="底色:").grid(row=2, column=0, sticky=tk.W, padx=(2, 4), pady=3)
        ttk.Entry(box, textvariable=self.bg_hex, width=11).grid(row=2, column=1, padx=2, sticky=tk.W)
        self.bg_swatch = tk.Label(box, text="   ", background="#FFFFFF", relief=tk.SUNKEN)
        self.bg_swatch.grid(row=2, column=2, sticky=tk.W, padx=(4, 0))
        ttk.Button(box, text="重新检测", command=self.detect_bg).grid(row=2, column=3, sticky=tk.W, padx=2)
        ttk.Label(box, text="默认取页面主色，与背景一致，可手工改",
                  foreground="#888888").grid(row=2, column=4, columnspan=3, sticky=tk.W, padx=(8, 0))

        # 页面信息
        self.size_label = ttk.Label(box, text="页面尺寸: --")
        self.size_label.grid(row=3, column=0, columnspan=7, sticky=tk.W, pady=(6, 0))
        self.range_label = ttk.Label(box, text="坐标范围: --")
        self.range_label.grid(row=4, column=0, columnspan=7, sticky=tk.W)

        ttk.Label(box,
                  text="坐标原点在【左上角】：X 向右增大，Y 向下增大（与 Acrobat / 福昕测量工具一致）",
                  foreground="#4a7ebb").grid(row=5, column=0, columnspan=7, sticky=tk.W, pady=(2, 0))

        ttk.Label(
            tab,
            text="擦除范围内全部内容：文字真正删除（不可再复制/搜索），图片只涂掉框内像素，\n"
                 "矢量图形按框裁剪；擦完填成上面的底色。当前一次处理一页，多页已预留接口。",
            foreground="#777777", justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

    # ---------------- 页面信息 ----------------
    def _unit_scale(self) -> float:
        """1 个显示单位等于多少 pt"""
        return PT_PER_MM if self.unit.get() == "mm" else 1.0

    def _fmt(self, pt_value: float, digits: int = 1) -> str:
        return f"{pt_value / self._unit_scale():.{digits}f}"

    def browse_input_pdf(self):
        path = filedialog.askopenfilename(filetypes=[("PDF Files", "*.pdf")])
        if path:
            self.input_pdf_path.set(path)
            if not self.output_pdf_path.get():
                base, ext = os.path.splitext(path)
                self.output_pdf_path.set(f"{base}_output{ext}")
            self.load_pdf_info()

    def browse_output_pdf(self):
        path = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF Files", "*.pdf")])
        if path:
            self.output_pdf_path.set(path)

    def load_pdf_info(self):
        """读取输入 PDF，刷新总页数、页面尺寸、坐标范围、底色"""
        path = self.input_pdf_path.get().strip()
        if not path or not os.path.exists(path):
            self._page_total, self._page_rect = 0, None
            self.page_total_label.config(text="页，共 0 页")
            self.size_label.config(text="页面尺寸: --")
            self.range_label.config(text="坐标范围: --")
            return
        try:
            doc = pymupdf.open(path)
        except Exception as e:
            logging.error(f"打开 PDF 失败: {e}")
            return

        try:
            self._page_total = len(doc)
            self.page_total_label.config(text=f"页，共 {self._page_total} 页")
            self.page_spin.config(to=max(1, self._page_total))
            page_no = max(1, min(self._page_total, int(self.page_no.get() or 1)))
            self.page_no.set(str(page_no))
            self._page_rect = pymupdf.Rect(doc[page_no - 1].rect)
            self.detect_bg(doc=doc, page_index=page_no - 1)
            logging.info(f"已载入 {os.path.basename(path)}：共 {self._page_total} 页，"
                         f"第 {page_no} 页尺寸 "
                         f"{self._page_rect.width:.1f} × {self._page_rect.height:.1f} pt")
        finally:
            doc.close()
        self._update_page_display()

    def _update_page_display(self):
        """按当前页码和单位刷新尺寸/坐标范围信息"""
        if self._page_rect is None:
            return
        try:
            page_no = int(self.page_no.get())
        except (TypeError, ValueError):
            return
        if not 1 <= page_no <= self._page_total:
            return

        w, h = self._page_rect.width, self._page_rect.height
        unit = self.unit.get()
        self.size_label.config(
            text=f"页面尺寸: {self._fmt(w)} × {self._fmt(h)} {unit}"
                 f"    （{w:.1f} × {h:.1f} pt = {w / PT_PER_MM:.1f} × {h / PT_PER_MM:.1f} mm）")
        self.range_label.config(
            text=f"坐标范围: X  0 ~ {self._fmt(w)}    Y  0 ~ {self._fmt(h)}    （单位 {unit}）")

    def detect_bg(self, doc=None, page_index=None):
        """自动取当前页主色作为擦除底色"""
        own_doc = None
        try:
            if doc is None:
                path = self.input_pdf_path.get().strip()
                if not path or not os.path.exists(path):
                    return
                own_doc = doc = pymupdf.open(path)
            if page_index is None:
                page_index = max(0, int(self.page_no.get() or 1) - 1)
                page_index = min(page_index, len(doc) - 1)
            self._auto_bg = detect_background_color(doc[page_index])
            hex_value = rgb_to_hex(self._auto_bg)
            self.bg_hex.set(hex_value)
            self.bg_swatch.config(background=hex_value)
            logging.info(f"检测到页面底色: {hex_value}")
        except Exception as e:
            logging.warning(f"底色检测失败，使用白色: {e}")
            self.bg_hex.set("#FFFFFF")
            self.bg_swatch.config(background="#FFFFFF")
        finally:
            if own_doc is not None:
                own_doc.close()

    # ---------------- 字体设置 ----------------
    def _update_font_scope_hint(self):
        """根据字体范围给出提示，让用户清楚现在会读到哪些字体"""
        if self.font_scope.get() == "all":
            text = "会读取系统字体（宋体/黑体/雅黑等），仅建议自用"
        else:
            text = "只用内置及 fonts 目录里的开源字体，产出可放心分发"
        self.font_scope_hint.config(text=text)

    def pick_font_file(self):
        """临时指定一个字体文件（本机自用系统字体时最方便，不用复制文件）"""
        path = filedialog.askopenfilename(
            title="选择字体文件",
            filetypes=[("字体文件", "*.ttf *.otf *.ttc"), ("所有文件", "*.*")],
            initialdir=r"C://Windows//Fonts" if os.path.isdir(r"C://Windows//Fonts") else None,
        )
        if not path:
            return
        self.custom_font = path
        self.font_file_label.config(text=os.path.basename(path), foreground="#333333")
        logging.info(f"已指定字体文件: {path}")

    def clear_font_file(self):
        self.custom_font = None
        self.font_file_label.config(text="未指定", foreground="#888888")
        logging.info("已清除自定义字体")

    # ---------------- 预览 ----------------
    def preview_rect(self):
        """把待擦除矩形画在页面副本上，弹窗预览"""
        path = self.input_pdf_path.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showerror("错误", "请先选择输入 PDF")
            return
        try:
            page_no = parse_page_number(self.page_no.get(), 10 ** 6)
            scale = self._unit_scale()
            x = float(self.rect_x.get()) * scale
            y = float(self.rect_y.get()) * scale
            w = float(self.rect_w.get()) * scale
            h = float(self.rect_h.get()) * scale
        except Exception as e:
            messagebox.showerror("参数错误", str(e))
            return

        doc = pymupdf.open(path)
        try:
            if not 1 <= page_no <= len(doc):
                messagebox.showerror("错误", f"页码超出范围：应为 1 ~ {len(doc)}")
                return
            page = doc[page_no - 1]
            rect = ui_rect_to_pdf_rect(x, y, w, h, page.rect.height)
            err = validate_rect(x, y, w, h, page.rect.width, page.rect.height)
            if err:
                messagebox.showerror("矩形不合法", err)
                return

            page.draw_rect(rect, color=(0.85, 0.1, 0.1), fill=(1, 0.2, 0.2),
                           width=1.5, fill_opacity=0.25, stroke_opacity=1.0)
            zoom = min(1.6, 900 / max(page.rect.width, 1))
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        finally:
            doc.close()

        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        pix.save(tmp_path)

        win = tk.Toplevel(self)
        win.title(f"预览 — 第 {page_no} 页")
        photo = tk.PhotoImage(file=tmp_path)
        label = ttk.Label(win, image=photo)
        label.image = photo          # 防止被回收
        label.pack(padx=8, pady=8)
        unit = self.unit.get()
        ttk.Label(win, text=f"红框 = 待擦除区域：左上角 ({self.rect_x.get()}, {self.rect_y.get()}) "
                            f"宽 {self.rect_w.get()} 高 {self.rect_h.get()} {unit}",
                  foreground="#aa3333").pack(pady=(0, 8))

        def _cleanup():
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _cleanup)

    # ---------------- 日志 ----------------
    def _create_log_widget(self, parent):
        log_text = tk.Text(parent, state='disabled', wrap=tk.WORD, height=7)
        log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(parent, command=log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        log_text['yscrollcommand'] = scrollbar.set

        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        gui_handler = TextHandler(log_text)
        gui_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(gui_handler)

    def get_resource_path(self, relative_path):
        """兼容旧调用，实际逻辑在模块级 resource_path()"""
        return resource_path(relative_path)

    # ---------------- 执行 ----------------
    def start_processing_thread(self):
        processing_thread = threading.Thread(target=self._dispatch, daemon=True)
        processing_thread.start()

    def _dispatch(self):
        if self.notebook is None:
            self.process_text_replace()
            return
        if self.notebook.index(self.notebook.select()) == 0:
            self.process_text_replace()
        else:
            self.process_erase()

    def _check_io_paths(self):
        input_pdf = self.input_pdf_path.get().strip()
        output_pdf = self.output_pdf_path.get().strip()
        if not all([input_pdf, output_pdf]):
            messagebox.showerror("错误", "输入/输出路径都必须填写！")
            return None
        if not os.path.exists(input_pdf):
            messagebox.showerror("错误", f"输入文件不存在: {input_pdf}")
            return None
        if os.path.abspath(input_pdf) == os.path.abspath(output_pdf):
            messagebox.showerror("错误", "输出文件不能与输入文件相同！")
            return None
        return input_pdf, output_pdf

    def process_text_replace(self):
        io = self._check_io_paths()
        if not io:
            return
        input_pdf, output_pdf = io
        rules_text = self.rules_text.get("1.0", tk.END)

        rules = {}
        for line_num, line in enumerate(rules_text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('|')
            if len(parts) != 2:
                messagebox.showerror("规则错误", f"规则第 {line_num} 行格式错误: {line}")
                return
            rules[parts[0].strip()] = parts[1].strip()

        if not rules:
            messagebox.showerror("错误", "没有有效的规则可供使用！")
            return

        try:
            logging.info("=" * 30)
            logging.info("开始文本替换...")
            replacer = PyMuPDFTextReplacer(rules)
            replacer.fonts_dirs = font_dirs()
            replacer.allow_system_fonts = (self.font_scope.get() == "all")
            replacer.extra_fonts = [self.custom_font] if self.custom_font else []
            logging.info(f"字体目录: {replacer.fonts_dirs}")
            logging.info(f"字体范围: {'含系统字体' if replacer.allow_system_fonts else '仅开源字体'}"
                         + (f"；自定义字体: {os.path.basename(self.custom_font)}"
                            if self.custom_font else ""))

            replacer.replace_pdf(input_pdf, output_pdf, method=self.method.get())

            if self.verify.get():
                failed_rules = verify_replacements(output_pdf, replacer.rules)
                if failed_rules:
                    logging.warning(f"有 {len(failed_rules)} 条规则未成功替换。")
                else:
                    logging.info("所有规则均已成功验证！")

            logging.info("处理全部完成！")
            messagebox.showinfo("成功", f"处理完成！\n输出文件保存在: {output_pdf}")
        except Exception as e:
            logging.error(f"发生严重错误: {e}")
            import traceback
            logging.error(traceback.format_exc())
            messagebox.showerror("严重错误", f"处理失败，请查看日志获取详情。\n错误: {e}")

    def process_erase(self):
        io = self._check_io_paths()
        if not io:
            return
        input_pdf, output_pdf = io

        try:
            scale = self._unit_scale()
            page_no = int(self.page_no.get())
            x = float(self.rect_x.get()) * scale
            y = float(self.rect_y.get()) * scale
            w = float(self.rect_w.get()) * scale
            h = float(self.rect_h.get()) * scale
        except (TypeError, ValueError):
            messagebox.showerror("参数错误", "页码、坐标、宽高必须是数字")
            return

        doc = pymupdf.open(input_pdf)
        try:
            total = len(doc)
            try:
                page_no = parse_page_number(str(page_no), total)
            except ValueError as e:
                messagebox.showerror("参数错误", str(e))
                return

            page = doc[page_no - 1]
            err = validate_rect(x, y, w, h, page.rect.width, page.rect.height)
            if err:
                messagebox.showerror("矩形不合法", err)
                return

            pdf_rect = ui_rect_to_pdf_rect(x, y, w, h, page.rect.height)
            page_height = page.rect.height
        finally:
            doc.close()

        try:
            logging.info("=" * 30)
            logging.info(f"开始区域擦除：第 {page_no} 页")
            logging.info(f"左上角 ({self.rect_x.get()}, {self.rect_y.get()}) "
                         f"宽 {self.rect_w.get()} 高 {self.rect_h.get()} {self.unit.get()}"
                         f"  → PDF 坐标 {pdf_rect}")
            try:
                color = hex_to_rgb(self.bg_hex.get())
            except ValueError as e:
                messagebox.showerror("底色错误", str(e))
                return
            logging.info(f"擦除底色: {rgb_to_hex(color)}")

            # 目前一页一个框；将来多页/多框时往 tasks 里追加即可
            tasks = [EraseTask(page_index=page_no - 1, rect=pdf_rect, color=color)]
            results = erase_document(input_pdf, output_pdf, tasks)
            for idx, used in results:
                logging.info(f"第 {idx + 1} 页：已擦除，底色 {rgb_to_hex(used)}")
            logging.info(f"完成，输出文件: {output_pdf}")
            messagebox.showinfo("成功", f"擦除完成！\n输出文件保存在: {output_pdf}")
        except Exception as e:
            logging.error(f"擦除失败: {e}")
            import traceback
            logging.error(traceback.format_exc())
            messagebox.showerror("严重错误", f"处理失败，请查看日志获取详情。\n错误: {e}")


# ---------------- 自检（打包后验证用） ----------------
def run_selftest() -> int:
    """不打开界面跑一遍中文替换和区域擦除，确认字体与核心链路可用。

    用法:  PDF工具箱.exe --selftest
    exe 是窗口程序没有控制台，所以结果同时写入 %TEMP%\\pdfbox_selftest.log。
    退出码 0 = 全部通过。
    """
    import shutil
    import tempfile

    lines = []

    def log(msg):
        lines.append(msg)
        try:
            print(msg)
        except Exception:
            pass

    def finish(code: int) -> int:
        log_path = os.path.join(tempfile.gettempdir(), "pdfbox_selftest.log")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            log(f"[自检] 结果已写入 {log_path}")
        except OSError:
            pass
        return code

    fonts_dirs = font_dirs()
    log(f"[自检] 字体目录: {fonts_dirs}")
    if not fonts_dirs:
        log("[自检] 失败：找不到 fonts 目录")
        return finish(2)

    font_files = []
    for d in fonts_dirs:
        font_files += sorted(f for f in os.listdir(d) if f.lower().endswith((".ttf", ".otf")))
    log(f"[自检] 字体文件: {font_files}")
    cjk_font = next((os.path.join(fonts_dirs[0], f) for f in font_files
                     if "NotoSansSC" in f), None)
    if not cjk_font:
        log("[自检] 失败：没有中文字体")
        return finish(2)

    tmp = tempfile.mkdtemp(prefix="pdfbox_selftest_")
    src = os.path.join(tmp, "in.pdf")
    out_replace = os.path.join(tmp, "out_replace.pdf")
    out_erase = os.path.join(tmp, "out_erase.pdf")

    # 造一个带中文和色块的测试页
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((50, 80), "旧公司名称 张三", fontfile=cjk_font,
                     fontname="NS", fontsize=14)
    page.draw_rect(pymupdf.Rect(40, 120, 200, 180), color=None,
                   fill=(0.9, 0.2, 0.2), width=0)
    doc.save(src)
    doc.close()

    ok = True

    # 1) 中文替换
    replacer = PyMuPDFTextReplacer({"旧公司名称": "新公司名称"})
    replacer.fonts_dirs = fonts_dirs
    replacer.allow_system_fonts = False
    replacer.replace_pdf(src, out_replace, method="precise")
    text = pymupdf.open(out_replace)[0].get_text()
    if "新公司名称" in text:
        log("[自检] 中文替换: 通过")
    else:
        log(f"[自检] 中文替换: 失败，输出文本 = {text!r}")
        ok = False

    # 2) 区域擦除
    rect = ui_rect_to_pdf_rect(30, 110, 190, 80)
    erase_document(src, out_erase, [EraseTask(page_index=0, rect=rect)])
    pixel = pymupdf.open(out_erase)[0].get_pixmap(dpi=72).pixel(100, 150)
    if pixel == (255, 255, 255):
        log("[自检] 区域擦除: 通过")
    else:
        log(f"[自检] 区域擦除: 失败，取样颜色 = {pixel}")
        ok = False

    shutil.rmtree(tmp, ignore_errors=True)
    log("[自检] 全部通过" if ok else "[自检] 存在失败项")
    return finish(0 if ok else 1)


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    App().mainloop()


if __name__ == '__main__':
    main()
