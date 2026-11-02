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
        self.title("PDF 文本替换 / 区域擦除工具")
        self.geometry("820x800")  # 稍后按内容自动调整

        # --- 变量 ---
        self.input_pdf_path = tk.StringVar()
        self.output_pdf_path = tk.StringVar()

        # 文本替换
        self.method = tk.StringVar(value='precise')
        self.verify = tk.BooleanVar(value=True)

        # 区域擦除
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

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=5)

        self._create_text_tab()
        self._create_erase_tab()

        run_button = ttk.Button(main_frame, text="开始处理", command=self.start_processing_thread)
        run_button.pack(pady=8, fill=tk.X)

        log_frame = ttk.LabelFrame(main_frame, text="日志", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self._create_log_widget(log_frame)

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
        current = self.notebook.select()
        need_h = self.winfo_reqheight()
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
    def _create_text_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="文本替换")

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
        ttk.Label(parent, text="替换方法:").pack(side=tk.LEFT, padx=5)
        methods = ['precise', 'overlay', 'hybrid']
        method_menu = ttk.OptionMenu(parent, self.method, self.method.get(), *methods)
        method_menu.pack(side=tk.LEFT, padx=5)

        verify_check = ttk.Checkbutton(parent, text="处理后验证结果", variable=self.verify)
        verify_check.pack(side=tk.RIGHT, padx=20)

    # ---------------- 区域擦除页 ----------------
    def _create_erase_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="区域擦除")

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
        """获取资源的绝对路径。
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

    # ---------------- 执行 ----------------
    def start_processing_thread(self):
        processing_thread = threading.Thread(target=self._dispatch, daemon=True)
        processing_thread.start()

    def _dispatch(self):
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
            replacer.fonts_dir = self.get_resource_path('fonts')
            logging.info(f"字体目录设置为: {replacer.fonts_dir}")

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


if __name__ == '__main__':
    app = App()
    app.mainloop()
