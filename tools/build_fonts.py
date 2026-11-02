# -*- coding: utf-8 -*-
"""
构建随程序分发的中文字体（开源可商用字体，无需系统商业字体）。

做两件事：
1. 把 Google Fonts 的可变字体（VF）实例化成静态字重（wght=400 Regular）；
2. 按常用字集裁剪字形，把体积从 17~25MB 压到几 MB。

用法：
    python tools/build_fonts.py

输入：fonts/_src/NotoSansSC-VF.ttf、fonts/_src/NotoSerifSC-VF.ttf
输出：fonts/NotoSansSC-Regular.ttf、fonts/NotoSerifSC-Regular.ttf

字体授权：Noto Sans SC / Noto Serif SC 均为 SIL Open Font License 1.1，
允许免费商用、嵌入文档、修改与再分发。
"""
import os
import sys

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools import subset


def build_charset() -> set:
    """常用字符集：ASCII + Latin-1 + GB2312 全部汉字 + 中日韩标点/全角符号"""
    codepoints = set(range(0x20, 0x7F))          # ASCII 可见字符
    codepoints |= set(range(0xA0, 0x100))        # Latin-1 补充（°±×÷ 等）
    codepoints |= set(range(0x2000, 0x2070))     # 通用标点（—“”…等）
    codepoints |= set(range(0x3000, 0x3040))     # 中日韩符号与标点（。、《》等）
    codepoints |= set(range(0xFF00, 0xFFF0))     # 全角 ASCII 与半角形式

    # GB2312：覆盖日常公文/合同/图纸里绝大多数汉字
    for hi in range(0xB0, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                ch = bytes([hi, lo]).decode("gb2312")
            except UnicodeDecodeError:
                continue
            codepoints.add(ord(ch))
    return codepoints


def make_static(src: str, dst: str, weight: int, codepoints: set) -> None:
    print(f"[1/3] 载入 {os.path.basename(src)} ...")
    font = TTFont(src, lazy=False)

    print(f"[2/3] 实例化 wght={weight} ...")
    if "fvar" in font:
        instantiateVariableFont(font, {"wght": weight}, inplace=True,
                                updateFontNames=True)

    # 子集化后是派生字体，名字里标注 Subset，避免与官方完整版混淆
    for rec in font["name"].names:
        if rec.nameID in (1, 4, 6, 16):
            original = rec.toUnicode()
            if "Subset" not in original:
                suffix = "Subset" if rec.nameID in (1, 16) else ""
                new = f"{original} {suffix}".strip() if suffix else original.replace(" ", "")
                try:
                    rec.string = new
                except Exception:
                    pass

    print(f"[3/3] 裁剪字形（{len(codepoints)} 个码位）...")
    options = subset.Options()
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.notdef_outline = True
    options.recalc_bounds = True
    options.drop_tables += ["DSIG"]
    options.layout_features = []          # PDF 嵌入不需要 OpenType 排印特性
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=codepoints)
    subsetter.subset(font)

    font.save(dst)
    font.close()
    size = os.path.getsize(dst) / 1048576
    print(f"  -> {dst}  {size:.2f} MB\n")


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    fonts_dir = os.path.join(os.path.dirname(here), "fonts")
    src_dir = os.path.join(fonts_dir, "_src")

    jobs = [
        ("NotoSansSC-VF.ttf", "NotoSansSC-Regular.ttf", 400),
        ("NotoSerifSC-VF.ttf", "NotoSerifSC-Regular.ttf", 400),
    ]

    missing = [j[0] for j in jobs if not os.path.exists(os.path.join(src_dir, j[0]))]
    if missing:
        print("缺少源字体（请先下载到 fonts/_src/）：")
        for m in missing:
            print("  -", m)
        print("\n下载地址：")
        print("  https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf")
        print("  https://raw.githubusercontent.com/google/fonts/main/ofl/notoserifsc/NotoSerifSC%5Bwght%5D.ttf")
        return 1

    codepoints = build_charset()
    for src_name, dst_name, weight in jobs:
        make_static(os.path.join(src_dir, src_name),
                    os.path.join(fonts_dir, dst_name), weight, codepoints)
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
