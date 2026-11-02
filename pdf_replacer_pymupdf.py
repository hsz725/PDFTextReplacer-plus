#!/usr/bin/env python3
"""
PDF文本替换脚本 - PyMuPDF优化版（最终兼容版）
使用PyMuPDF的精确文本定位和替换功能，保留原始字体和样式，并支持加载本地字体。
"""

import sys
import os
import time
import argparse
from typing import Dict, List, Tuple
import logging
import fitz  # PyMuPDF
import tempfile
import shutil

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ======================= 字体支持（中文显示修复） =======================
# PDF 内置的 14 种标准字体（helv / cour / timo ...）只覆盖 Latin-1 字符集，
# 完全没有汉字字形。一旦把中文交给它们写入，就会渲染成 "···"（缺字形替身）。
# 因此：只要新文本里出现 U+00FF 以上的字符，就必须换成带汉字字形的 TrueType 字体。
LATIN_SAFE_MAX = 0xFF

BUILTIN_FONTS = ["helv", "cour", "timo", "symb", "zadb", "times",
                 "courier", "helvetica", "symbol", "zapfdingbats", "tiro"]

# ============================ 授权声明 ============================
# 本程序只使用开源可商用字体（SIL Open Font License 1.1：可免费商用、
# 可嵌入文档、可再分发）：
#     Noto Sans SC  —— 等同思源黑体，黑体风格
#     Noto Serif SC —— 等同思源宋体，宋体风格
# 不再调用 Windows 自带的宋体(SimSun)/黑体(SimHei)/微软雅黑(MS YaHei)/
# 等线(DengXian) 等字体：这些字体受微软及其字体代工厂的商业授权约束，
# 随程序分发或嵌入商用文档都存在授权风险。
# 想换成别的开源字体，直接把 .ttf/.otf 放进 fonts 目录即可自动优先使用。
FONT_FILE_EXTS = (".ttf", ".otf", ".ttc", ".otc")

# 原文字体名 → 期望的字体风格关键词。
# 左边是"原文字体名里出现这些词"，右边是"候选字体**内部名称**里出现这些词"。
# 注意右边要按字体真实名称来写：SimSun 的家族名是 "SimSun Regular"，
# 里面既没有 song 也没有 ming，所以必须写 sun。
_FONT_STYLE_HINTS = [
    (("song", "sun", "宋", "ming", "明朝", "serif", "fang", "仿"),
     ("serif", "song", "sun", "ming", "宋")),
    (("hei", "黑", "yahei", "雅黑", "deng", "等线", "sans", "gothic"),
     ("sans", "hei", "黑", "deng")),
    (("kai", "楷"), ("kai", "楷")),
]

# 粗体/细体等非正文子重，仅在别无选择时才用
_NON_REGULAR_MARKS = ("bold", "light", "thin", "black", "heavy",
                      "medium", "semibold", "demibold", "italic", "oblique")

# 系统中可能存在的商业中文字体（Windows / macOS 自带，受厂商授权约束）。
# 默认**不读取**；只有用户在界面上主动选择"使用所有字体"时才会用到。
# 本机自用没问题，但绝不能随程序分发，也不要把用这些字体生成的构建发给别人。
_SYSTEM_FONTS = [
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
    r"C:\Windows\Fonts\Deng.ttf",       # 等线
    r"C:\Windows\Fonts\simkai.ttf",     # 楷体
    r"C:\Windows\Fonts\simfang.ttf",    # 仿宋
    "/System/Library/Fonts/PingFang.ttc",
]

# 系统中可能存在的开源中文字体（均为 OFL 授权），仅作兜底。
# 注意：刻意不包含任何 Windows / macOS 自带商业字体。
_OPEN_LICENSE_CJK_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",   # 文泉驿微米黑
]


def needs_unicode_font(text: str) -> bool:
    """文本里是否有超出 Latin-1 的字符（汉字、日文、全角标点等）"""
    return any(ord(ch) > LATIN_SAFE_MAX for ch in text)


def _font_covers(font, text: str) -> bool:
    """字体是否覆盖 text 里的每一个有效字符"""
    for ch in text:
        if ch.isspace() or ord(ch) < 0x20:
            continue
        try:
            if not font.has_glyph(ord(ch)):
                return False
        except Exception:
            return False
    return True


def _list_font_files(fonts_dir: str) -> List[str]:
    """列出 fonts 目录下的字体文件名（只取文件，跳过 _src 之类的源码目录）"""
    files = []
    if not fonts_dir or not os.path.isdir(fonts_dir):
        return files
    try:
        for name in sorted(os.listdir(fonts_dir)):
            path = os.path.join(fonts_dir, name)
            if os.path.isfile(path) and name.lower().endswith(FONT_FILE_EXTS):
                files.append(name)
    except OSError:
        pass
    return files


def _normalize_dirs(fonts_dir) -> List[str]:
    """fonts_dir 可以是单个路径、路径列表或 None，统一成列表"""
    if not fonts_dir:
        return []
    if isinstance(fonts_dir, (list, tuple)):
        return [d for d in fonts_dir if d]
    return [fonts_dir]


def _wanted_style_keys(fontname_hint: str) -> tuple:
    """按原文字体名判断想要哪种风格，返回该风格的关键词元组；判断不出返回空元组"""
    hint = (fontname_hint or "").lower()
    for keys, wanted in _FONT_STYLE_HINTS:
        if any(k in hint for k in keys):
            return wanted
    return ()


def find_font_for_text(text: str, fontname_hint: str = "", fonts_dir=None,
                       extra_files=None, allow_system_fonts: bool = False):
    """为 text 挑一个能正常显示它的字体文件，找不到返回 None。

    参数：
        fonts_dir        —— 字体目录，可以传一个路径，也可以传路径列表。
                            列表按顺序查找，这样可以实现"用户自定义目录优先、
                            程序内置目录兜底"，用户放个空目录也不会把内置字体屏蔽掉。
        extra_files      —— 用户临时指定的字体文件（"选择字体文件"选中的），优先级最高。
        allow_system_fonts —— 是否允许使用系统自带字体（宋体/黑体/雅黑等商业字体）。
                            默认 False，只走开源字体；为 True 时才把系统字体纳入候选。

    挑选顺序：
        1. 用户指定的字体文件
        2. 目录里风格匹配的字体（按字体**内部名称**匹配宋/黑/楷，不看文件名——
           simsun.ttc 这种文件名里根本没有 "song"，按文件名匹配会认错）
        3. 目录里其它正文字体
        4. 系统里的开源中文字体（兜底）
        5. allow_system_fonts 为 True 时，才加上系统商业字体
    每一档都用 has_glyph 实际校验字形覆盖，覆盖不全就继续找下一个。
    """
    dirs = _normalize_dirs(fonts_dir)

    # ① 用户明确指定的字体文件：直接用它，不参与风格排序。
    #    用户既然亲手选了，就不该被"风格匹配"顶掉——之前就出过这个 bug：
    #    选了 msyh.ttc，结果因为原文是宋体，被换成了内置的思源宋体。
    for path in (extra_files or []):
        if not path or not os.path.exists(path):
            continue
        try:
            font = fitz.Font(fontfile=path)
        except Exception:
            continue
        if _font_covers(font, text):
            return path

    # ② 目录内候选（按目录顺序、Regular 优先）→ ③ 系统开源字体 → ④ 系统商业字体
    candidates: List[str] = []
    for d in dirs:
        names = _list_font_files(d)
        regular = [f for f in names if not any(m in f.lower() for m in _NON_REGULAR_MARKS)]
        others = [f for f in names if f not in regular]
        candidates += [os.path.join(d, f) for f in regular + others]

    candidates += _OPEN_LICENSE_CJK_FONTS
    if allow_system_fonts:
        candidates += _SYSTEM_FONTS

    hint = (fontname_hint or "").lower()
    hint_key = "".join(ch for ch in hint if ch.isalnum())
    wanted = _wanted_style_keys(fontname_hint)

    # 逐个体检：能读出来、且字形覆盖完整的才留下，并按匹配强度分三档
    strong: List[str] = []      # 字体名就是原文用那个（SimHei → SimHei Regular）
    matched: List[str] = []     # 同风格（宋→宋、黑→黑）
    fallback: List[str] = []    # 能显示就行
    seen = set()
    for path in candidates:
        if path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        try:
            font = fitz.Font(fontfile=path)
        except Exception:
            continue
        if not _font_covers(font, text):
            continue

        family = (getattr(font, "name", "") or "").lower()
        family_key = "".join(ch for ch in family if ch.isalnum())
        if len(hint_key) >= 4 and hint_key in family_key:
            strong.append(path)
        elif wanted and any(w in family for w in wanted):
            matched.append(path)
        else:
            fallback.append(path)

    ordered = strong + matched + fallback
    return ordered[0] if ordered else None


def _font_alias(path: str) -> str:
    """给字体文件生成一个合法的 PDF 资源名"""
    stem = os.path.splitext(os.path.basename(path))[0]
    safe = "".join(ch for ch in stem if ch.isalnum()) or "CJK"
    return f"{safe}{abs(hash(path)) % 10000}"


class PyMuPDFTextReplacer:
    """使用PyMuPDF的文本替换器"""

    def __init__(self, rules_source: str | Dict[str, str]):
        """
        初始化替换器

        Args:
            rules_source: 替换规则，可以是文件路径(str)或规则字典(dict)
        """
        if isinstance(rules_source, dict):
            self.rules = rules_source
            logger.info(f"从字典加载了 {len(self.rules)} 条替换规则")
        else:
            self.rules = self._load_rules_from_file(rules_source)
            logger.info(f"从文件 {rules_source} 加载了 {len(self.rules)} 条替换规则")

        # 字体目录列表（只读查找，不主动创建）。
        # 用列表是为了支持"用户目录优先 + 内置目录兜底"：用户即使在程序旁边
        # 建了个空 fonts 目录，也不会把程序内置的字体屏蔽掉。
        self.fonts_dirs: List[str] = ["fonts"]

        # 用户临时指定的字体文件（界面上"选择字体文件"挑的），优先级最高。
        # 用于本机自用系统字体，不作为分发内容。
        self.extra_fonts: List[str] = []

        # 是否允许使用系统自带的商业中文字体（宋体/黑体/雅黑等）。
        # 默认 False：只走开源字体，保证产出的 PDF 可以放心分发。
        # 用户在界面上选"使用所有字体"时才置 True。
        self.allow_system_fonts: bool = False

        # 字体解析缓存 {fontname提示 + 文本: 字体文件路径}
        self._font_path_cache: Dict[str, str | None] = {}

    @property
    def fonts_dir(self):
        """兼容旧的单目录用法（读写第一个目录）"""
        return self.fonts_dirs[0] if self.fonts_dirs else ""

    @fonts_dir.setter
    def fonts_dir(self, value):
        self.fonts_dirs = [value] if value else []
        self._font_path_cache.clear()

    def _load_rules_from_file(self, rules_file: str) -> Dict[str, str]:
        """
        从文件加载替换规则

        Args:
            rules_file: 规则文件路径

        Returns:
            替换规则字典 {原文本: 替换文本}
        """
        rules = {}
        try:
            with open(rules_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split('|')
                    if len(parts) != 2:
                        logger.warning(f"第 {line_num} 行格式错误，跳过: {line}")
                        continue
                    rules[parts[0].strip()] = parts[1].strip()
        except Exception as e:
            logger.error(f"读取规则文件失败: {e}")
            raise
        return rules

    def replace_pdf(self, input_pdf: str, output_pdf: str, method: str = 'precise'):
        """
        执行PDF文本替换

        Args:
            input_pdf: 输入PDF文件路径
            output_pdf: 输出PDF文件路径
            method: 替换方法 ('precise', 'overlay', 'hybrid')
        """
        start_time = time.time()
        try:
            # 先确认中文字体齐备，避免擦掉原文却写不进去，产出满屏"···"的废文件
            self._preflight_check_fonts()

            if method == 'precise':
                total_replacements = self._precise_replace_fixed(input_pdf, output_pdf)
            elif method == 'overlay':
                total_replacements = self._overlay_replace(input_pdf, output_pdf)
            elif method == 'hybrid':
                total_replacements = self._hybrid_replace(input_pdf, output_pdf)
            else:
                logger.error(f"未知的替换方法: {method}")
                return

            elapsed_time = time.time() - start_time
            logger.info(f"处理完成！")
            logger.info(f"总计替换: {total_replacements} 处")
            logger.info(f"耗时: {elapsed_time:.2f} 秒")
            logger.info(f"输出文件: {output_pdf}")

        except Exception as e:
            logger.error(f"处理PDF时出错: {e}")
            raise

    def _find_local_font(self, font_name: str) -> str | None:
        """在所有字体目录里按文件名前缀查找字体文件"""
        for d in self.fonts_dirs:
            if not d or not os.path.isdir(d):
                continue
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for ext in ['.ttf', '.otf', '.ttc']:
                for f in names:
                    if f.lower().startswith(font_name.lower()) and f.lower().endswith(ext):
                        return os.path.join(d, f)
        return None

    def _resolve_font(self, text: str, fontname_hint: str) -> str | None:
        """带缓存地为 text 找一个能显示它的字体文件"""
        key = f"{fontname_hint}\x00{text}"
        if key not in self._font_path_cache:
            path = find_font_for_text(text, fontname_hint, self.fonts_dirs,
                                      extra_files=self.extra_fonts,
                                      allow_system_fonts=self.allow_system_fonts)
            if path:
                logger.info(f"文本 '{text[:20]}' 使用字体: {os.path.basename(path)}")
            else:
                logger.warning(f"未找到能显示 '{text[:20]}' 的字体，"
                               f"请往 fonts 目录放入一个中文字体（.ttf/.otf）")
            self._font_path_cache[key] = path
        return self._font_path_cache[key]

    def _preflight_check_fonts(self) -> None:
        """替换前先确认所有含中文的新文本都能找到字体。

        否则原文字已经被擦掉、新文字又写不进去，只会留下一串"···"的废文件。
        """
        missing = [t for t in self.rules.values()
                   if needs_unicode_font(t)
                   and not find_font_for_text(t, "", self.fonts_dirs,
                                              extra_files=self.extra_fonts,
                                              allow_system_fonts=self.allow_system_fonts)]
        if missing:
            sample = "、".join(t[:15] for t in missing[:5])
            more = f" 等 {len(missing)} 条" if len(missing) > 5 else ""
            dirs = "、".join(os.path.abspath(d) for d in self.fonts_dirs) or "（未配置）"
            raise RuntimeError(
                f"缺少可用的中文字体，无法写入：{sample}{more}。\n"
                f"请把开源中文字体（.ttf/.otf，例如思源黑体 / Noto Sans SC）"
                f"放入字体目录：{dirs}\n"
                f"或在界面「选项」里点「选择字体文件…」指定一个字体，"
                f"也可以把「字体范围」切成「使用所有字体」以读取系统字体。"
            )

    @staticmethod
    def _subset_fonts(doc) -> None:
        """保存前做字体子集化：只保留实际用到的字形，
        否则每份输出 PDF 都会把整份中文字体（2~3MB）塞进去。"""
        try:
            doc.subset_fonts()
        except Exception as e:
            logger.debug(f"字体子集化跳过（不影响结果）: {e}")

    def _write_text(self, page, x: float, baseline: float, text: str,
                    fontname: str, fontsize: float, color) -> bool:
        """
        把 text 写到 (x, baseline)，自动挑选能覆盖该文本的字体。

        关键修复：原代码查不到字体时一律回退到内置 'helv'，而 helv 没有汉字字形，
        所以中文替换结果会变成 "···"。这里改为：只要含非 Latin-1 字符，
        就强制使用一个带汉字字形的 TrueType 字体。
        """
        point = fitz.Point(x, baseline)

        # ① 含中文等非 Latin-1 字符：必须用带汉字字形的 TrueType 字体
        if needs_unicode_font(text):
            path = self._resolve_font(text, fontname)
            if path:
                alias = _font_alias(path)
                try:
                    page.insert_font(fontfile=path, fontname=alias)
                    page.insert_text(point, text, fontname=alias,
                                     fontsize=fontsize, color=color)
                    return True
                except Exception as e:
                    logger.warning(f"用 {os.path.basename(path)} 写入失败({e})，尝试其它方式")

        # ② 纯西文文本：沿用原有逻辑（优先使用 fonts 目录里的同名字体）
        font_to_use, font_file_path = fontname, None
        if font_to_use.lower().split("-")[0] not in BUILTIN_FONTS:
            font_file_path = self._find_local_font(font_to_use)
            if not font_file_path:
                logger.warning(f"字体 '{font_to_use}' 未找到，改用 'helv'")
                font_to_use = "helv"

        try:
            if font_file_path:
                page.insert_font(fontfile=font_file_path, fontname=font_to_use)
            page.insert_text(point, text, fontname=font_to_use,
                             fontsize=fontsize, color=color)
            return True
        except Exception as e:
            logger.error(f"写入文本 '{text}' 失败: {e}")
            return False

    def _precise_replace_fixed(self, input_pdf: str, output_pdf: str) -> int:
        """
        修复版精确替换方法：采用“查找-擦除-写入”三步法，并精确对齐基线。
        """
        logger.info("使用修复版精确替换方法...")
        total_replacements = 0

        doc = fitz.open(input_pdf)
        logger.info(f"打开PDF文件成功，共 {len(doc)} 页")

        for page_num, page in enumerate(doc):

            # 1. 查找：收集所有需要替换的动作和位置信息
            actions = []
            for old_text, new_text in self.rules.items():
                text_instances = page.search_for(old_text)
                for inst in text_instances:
                    text_dict = page.get_text("dict", clip=inst)
                    fontname, fontsize, text_color = "helv", 12, (0, 0, 0)
                    # 核心改动：默认基线为矩形框底部，但会尝试寻找更精确的
                    baseline = inst.y1

                    # 提取字体样式和精确基线
                    for block in text_dict["blocks"]:
                        if block["type"] == 0:
                            for line in block["lines"]:
                                for span in line["spans"]:
                                    if old_text in span["text"]:
                                        fontname = span.get("font", "helv").split("+")[-1]
                                        fontsize = span.get("size", 12)
                                        baseline = span.get("origin")[1]  # 获取精确基线Y坐标
                                        color = span.get("color", 0)
                                        if isinstance(color, int):
                                            text_color = (((color >> 16) & 0xFF) / 255.0, ((color >> 8) & 0xFF) / 255.0,
                                                          (color & 0xFF) / 255.0)
                                        break

                    actions.append({
                        "rect": inst, "new_text": new_text, "fontname": fontname,
                        "fontsize": fontsize, "color": text_color, "baseline": baseline
                    })

            if not actions:
                continue

            # 2. 擦除：将所有找到的旧文本区域标记为空白并应用
            for action in actions:
                page.add_redact_annot(action["rect"], text="")

            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

            # 3. 写入：在擦除后的空白区域写入新文本
            for action in actions:
                # 使用精确的插入点 (rect.x0, baseline)，字体由 _write_text 自动挑选
                self._write_text(page, action["rect"].x0, action["baseline"],
                                 action["new_text"], action["fontname"],
                                 action["fontsize"], action["color"])

            total_replacements += len(actions)
            logger.info(f"页面 {page_num + 1}: 完成 {len(actions)} 处替换")

        self._subset_fonts(doc)
        doc.save(output_pdf, garbage=4, deflate=True, clean=True)
        doc.close()
        return total_replacements

    def _overlay_replace(self, input_pdf: str, output_pdf: str) -> int:
        """
        覆盖替换方法：使用白色矩形覆盖原文本，然后插入新文本
        """
        logger.info("使用覆盖替换方法...")
        total_replacements = 0
        shutil.copy2(input_pdf, output_pdf)
        doc = fitz.open(output_pdf)

        for page_num, page in enumerate(doc):
            page_replacements = 0
            replacements = []

            for old_text, new_text in self.rules.items():
                text_instances = page.search_for(old_text)
                for inst in text_instances:
                    text_dict = page.get_text("dict", clip=inst)
                    font_info = {'fontname': 'helv', 'fontsize': 12, 'color': (0, 0, 0), 'baseline': inst.y1}
                    for block in text_dict["blocks"]:
                        if block["type"] == 0:
                            for line in block["lines"]:
                                for span in line["spans"]:
                                    if old_text in span["text"]:
                                        font_info['fontname'] = span.get("font", "helv").split("+")[-1]
                                        font_info['fontsize'] = span.get("size", 12)
                                        font_info['baseline'] = span.get("origin")[1]
                                        color = span.get("color", 0)
                                        if isinstance(color, int):
                                            font_info['color'] = (((color >> 16) & 0xFF) / 255.0,
                                                                  ((color >> 8) & 0xFF) / 255.0, (color & 0xFF) / 255.0)
                                        break
                    replacements.append({'rect': inst, 'new_text': new_text, 'font_info': font_info})

            if replacements:
                replacements.sort(key=lambda x: (x['rect'].y0, x['rect'].x0), reverse=True)
                for repl in replacements:
                    rect = fitz.Rect(repl['rect'])
                    font_info = repl['font_info']
                    shape = page.new_shape()
                    shape.draw_rect(rect)
                    shape.finish(color=(1, 1, 1), fill=(1, 1, 1), width=0)
                    shape.commit()
                    insert_point = fitz.Point(rect.x0, font_info['baseline'])
                    self._write_text(page, rect.x0, font_info['baseline'],
                                     repl['new_text'], font_info['fontname'],
                                     font_info['fontsize'], font_info['color'])
                    page_replacements += 1

            if page_replacements > 0:
                total_replacements += page_replacements
                logger.info(f"页面 {page_num + 1}: 完成 {page_replacements} 处替换")

        # 修复：doc 就是 output_pdf 本身，不能直接以非增量方式覆盖保存，
        # 否则 PyMuPDF 报 "save to original must be incremental"。先存临时文件再替换。
        tmp_out = output_pdf + ".tmp"
        self._subset_fonts(doc)
        doc.save(tmp_out, garbage=4, deflate=True)
        doc.close()
        os.replace(tmp_out, output_pdf)
        return total_replacements

    def _hybrid_replace(self, input_pdf: str, output_pdf: str) -> int:
        """
        混合方法：先尝试精确替换，失败则使用覆盖方法
        """
        logger.info("使用混合替换方法...")
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            replacements = self._precise_replace_fixed(input_pdf, tmp_path)
            if self._verify_replacements(tmp_path):
                shutil.move(tmp_path, output_pdf)
                return replacements
            else:
                logger.warning("精确替换未完全成功，使用覆盖方法")
                os.remove(tmp_path)
                return self._overlay_replace(input_pdf, output_pdf)
        except Exception as e:
            logger.error(f"混合替换失败: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def _verify_replacements(self, pdf_path: str) -> bool:
        """快速验证替换是否成功"""
        try:
            doc = fitz.open(pdf_path)
            for page in doc:
                for old_text in self.rules.keys():
                    if page.search_for(old_text):
                        doc.close()
                        return False
            doc.close()
            return True
        except:
            return False


def verify_replacements(pdf_path: str, rules: Dict[str, str]):
    """验证替换结果"""
    logger.info("\n验证替换结果...")
    doc = fitz.open(pdf_path)
    success_count, failed_rules = 0, []

    for old_text, new_text in rules.items():
        found_old, found_new = False, False
        for page in doc:
            if page.search_for(old_text): found_old = True
            if page.search_for(new_text): found_new = True
        if found_new and not found_old:
            success_count += 1
            logger.info(f"✓ 成功替换: {old_text} -> {new_text}")
        elif found_old:
            failed_rules.append(old_text)
            logger.warning(f"✗ 未替换: {old_text} (仍然存在)")
        else:
            logger.info(f"- 未找到: {old_text} (原文本不存在)")
    doc.close()
    logger.info(f"\n验证完成: {success_count}/{len(rules)} 规则成功")
    return failed_rules


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='PDF文本替换工具（PyMuPDF修复版）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
替换方法说明:
  precise - 精确替换（推荐）：使用标记功能，保持原始字体，并支持从'fonts'目录加载本地字体。
  overlay - 覆盖替换：白色覆盖+重新插入，更可靠但可能有细微差异。
  hybrid  - 混合替换：自动选择最佳方法。

示例:
  python pdf_replacer_pymupdf.py input.pdf output.pdf rules.txt
  python pdf_replacer_pymupdf.py input.pdf output.pdf rules.txt --method overlay
  python pdf_replacer_pymupdf.py input.pdf output.pdf rules.txt --verify
        """
    )
    parser.add_argument('input_pdf', help='输入PDF文件路径')
    parser.add_argument('output_pdf', help='输出PDF文件路径')
    parser.add_argument('rules_file', help='替换规则文件路径')
    parser.add_argument('--method', choices=['precise', 'overlay', 'hybrid'], default='precise',
                        help='替换方法（默认: precise）')
    parser.add_argument('--verify', action='store_true', help='验证替换结果')
    args = parser.parse_args()

    if not os.path.exists(args.input_pdf):
        logger.error(f"输入文件不存在: {args.input_pdf}")
        sys.exit(1)
    if not os.path.exists(args.rules_file):
        logger.error(f"规则文件不存在: {args.rules_file}")
        sys.exit(1)
    if os.path.abspath(args.input_pdf) == os.path.abspath(args.output_pdf):
        logger.error("输出文件不能与输入文件相同！")
        sys.exit(1)

    try:
        replacer = PyMuPDFTextReplacer(args.rules_file)
        replacer.replace_pdf(args.input_pdf, args.output_pdf, method=args.method)
        if args.verify:
            failed_rules = verify_replacements(args.output_pdf, replacer.rules)
            if failed_rules:
                logger.warning(f"\n有 {len(failed_rules)} 条规则未成功替换")
                logger.info("建议尝试 --method overlay 或 --method hybrid")
    except Exception as e:
        logger.error(f"处理失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()