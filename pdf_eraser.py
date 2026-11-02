# -*- coding: utf-8 -*-
"""
区域擦除（只擦除、不替换）核心逻辑。

一、坐标系约定
    面向用户：原点在**页面左上角**，X 向右增大、Y 向下增大。
    这是 Acrobat / 福昕等阅读器的测量、裁剪工具采用的约定，也跟屏幕坐标一致，
    对操作者最直观。
    注意三套坐标系不要混：
      · PDF 文件格式规范（ISO 32000）：左下角原点、Y 向上
      · PyMuPDF（MuPDF）API：**左上角原点、Y 向下** —— 本程序直接用的就是这套
      · 界面显示：左上角原点、Y 向下 —— 与 PyMuPDF 完全一致，无需翻转
    也就是说，界面上量的坐标可以原样交给 PyMuPDF 用；真正要翻转到 PDF 文件
    内部坐标的那一步，由 PyMuPDF 自己处理。

二、擦除策略（实测过几种组合后的选择）
    text      -> 真正删除文字，之后无法再选中/复制/搜索
    images    -> PDF_REDACT_IMAGE_PIXELS：只把落在框内的像素涂成底色，
                 图片其余部分原样保留（不会整张图被删掉）
    graphics  -> PDF_REDACT_LINE_ART_REMOVE_IF_COVERED：完全落在框内的矢量对象
                 物理删除；跨出框的保留，由底色填充盖住。
                 不用 REMOVE_IF_TOUCHED，否则像"整页背景矩形"这种大对象会被
                 整个删掉，擦完出现一块白斑。
"""

from dataclasses import dataclass
from collections import Counter
from typing import Iterable, List, Optional, Sequence, Tuple

import pymupdf

RGB = Tuple[float, float, float]


def ui_rect_to_pdf_rect(x: float, y: float, width: float, height: float,
                        page_height: float = None) -> pymupdf.Rect:
    """把"左上角坐标 + 宽高"换算成 PyMuPDF 的页面矩形。

    PyMuPDF 的页面坐标系本身就是左上角原点、Y 向下，和界面上的输入完全一致，
    因此这里不做任何翻转。page_height 参数只为兼容旧调用保留，不参与计算。

    （PDF 文件格式本身是左下角原点、Y 向上，但那层转换由 PyMuPDF 内部完成，
      业务代码不该再翻一次——翻两次就会正好上下颠倒。）
    """
    return pymupdf.Rect(x, y, x + width, y + height)


def detect_background_color(page: "pymupdf.Page", dpi: int = 36) -> RGB:
    """取页面上出现次数最多的颜色，作为"背景色"。

    低分辨率渲染后统计主色，比固定用白色更靠谱——很多图纸/表单的背景是
    米色、浅灰或企业色，直接用白色擦除会留下一块显眼的补丁。
    """
    try:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    except Exception:
        return (1.0, 1.0, 1.0)

    n = pix.n
    samples = pix.samples
    total = pix.width * pix.height
    if not total:
        return (1.0, 1.0, 1.0)

    # 最多取 2 万个采样点，够判断主色又不拖慢速度
    step = max(1, total // 20000)
    counter: Counter = Counter()
    stride = n * step
    for i in range(0, len(samples) - n + 1, stride):
        counter[samples[i:i + n]] += 1

    if not counter:
        return (1.0, 1.0, 1.0)
    rgb_bytes, _ = counter.most_common(1)[0]
    return tuple(v / 255.0 for v in rgb_bytes[:3])  # type: ignore[return-value]


def rgb_to_hex(color: RGB) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(c * 255))) for c in color)


def hex_to_rgb(text: str) -> RGB:
    """解析 #RRGGBB 或 RRGGBB"""
    s = (text or "").strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(f"颜色格式应为 #RRGGBB，收到: {text!r}")
    return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


@dataclass
class EraseTask:
    """一条擦除任务。

    目前界面一次只产出一条，但这里用列表承载，是为了将来支持
    "一次擦除多页 / 一页擦多个框" 时不用改底层逻辑。
    """
    page_index: int                     # 0 基页序号
    rect: pymupdf.Rect                  # PDF 坐标（左下原点）
    color: Optional[RGB] = None         # 底色，None 表示擦除时自动检测


def erase_on_page(page: "pymupdf.Page", rect: pymupdf.Rect,
                  color: Optional[RGB] = None) -> RGB:
    """在单页上擦掉 rect 范围内的所有内容，返回实际使用的底色"""
    used_color = color or detect_background_color(page)
    page.add_redact_annot(rect, fill=used_color)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
        graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )
    return used_color


def erase_document(input_pdf: str, output_pdf: str,
                   tasks: Sequence[EraseTask],
                   progress=None) -> List[Tuple[int, RGB]]:
    """执行一批擦除任务并保存。

    Args:
        progress: 可选回调 (已完成数, 总数)，供界面刷新进度
    Returns:
        [(页序号(0基), 实际底色), ...]
    """
    results: List[Tuple[int, RGB]] = []
    doc = pymupdf.open(input_pdf)
    try:
        for i, task in enumerate(tasks, 1):
            if not 0 <= task.page_index < len(doc):
                raise ValueError(f"页码超出范围: {task.page_index + 1}（共 {len(doc)} 页）")
            page = doc[task.page_index]
            used = erase_on_page(page, task.rect, task.color)
            results.append((task.page_index, used))
            if progress:
                progress(i, len(tasks))
        doc.save(output_pdf, garbage=4, deflate=True, clean=True)
    finally:
        doc.close()
    return results


def validate_rect(x: float, y: float, width: float, height: float,
                  page_width: float, page_height: float) -> Optional[str]:
    """校验矩形是否合法，返回错误说明；合法返回 None"""
    if width <= 0 or height <= 0:
        return "宽和高必须大于 0"
    if x < 0 or y < 0:
        return "左上角坐标不能为负数"
    if x + width > page_width + 1e-6:
        return f"矩形右边超出页面宽度（{x + width:.1f} > {page_width:.1f}）"
    if y + height > page_height + 1e-6:
        return f"矩形下边超出页面高度（{y + height:.1f} > {page_height:.1f}）"
    return None


def parse_page_number(text: str, total: int) -> int:
    """解析页码输入。

    目前只接受单页（1..total）。将来要支持 "1,3,5" / "2-6" 这类写法时，
    在这里扩展成返回列表即可，调用方已经是按 EraseTask 列表驱动的。
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("请填写页码")
    if not s.isdigit():
        raise ValueError(f"页码必须是正整数，收到: {text!r}")
    n = int(s)
    if not 1 <= n <= total:
        raise ValueError(f"页码超出范围：应为 1 ~ {total}，收到 {n}")
    return n
