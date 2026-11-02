# -*- coding: utf-8 -*-
"""一键打包脚本：同一份源码产出两个版本的单文件 exe。

    python tools/build.py            # 两个版本都打
    python tools/build.py --lite     # 只打精简版
    python tools/build.py --full     # 只打完整版

产出（都放在仓库根目录，旁边不需要任何附带文件）：
    PDF工具箱.exe       文本替换 + 区域擦除
    PDF文本替换工具.exe  仅文本替换

注意几个坑：
  1. --add-data 的源路径必须是绝对路径，因为用了 --specpath 之后相对路径
     会相对 spec 目录解析。
  2. 不能直接打包 fonts 目录：里面有 _src（40MB+ 的原始可变字体）。
     所以先挑出要分发的几个文件到 build/fonts_pack。
  3. PyMuPDF 2.0 会带上 onnxruntime / pymupdf_layout / pymupdf4llm 等
     版面分析用的大包，排除后体积从 66MB 降到 44MB。
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_DIR = os.path.join(ROOT, "build")
FONTS_SRC = os.path.join(ROOT, "fonts")
FONTS_PACK = os.path.join(BUILD_DIR, "fonts_pack")

# 需要随程序分发的字体文件（不含 _src 里的原始可变字体）
FONT_FILES = [
    "NotoSansSC-Regular.ttf",
    "NotoSerifSC-Regular.ttf",
    "LiberationSans-Bold.ttf",
    "OFL.txt",
    "README.md",
]

# 排除的无用大依赖
EXCLUDES = [
    "onnxruntime", "pymupdf_layout", "pymupdf4llm", "networkx",
    "protobuf", "flatbuffers", "tabulate", "psutil", "yaml",
    "PIL", "fontTools", "unittest", "pydoc",
]

TARGETS = {
    "full": {"entry": os.path.join("tools", "entry_full.py"), "name": "PDF工具箱"},
    "lite": {"entry": os.path.join("tools", "entry_lite.py"), "name": "PDF文本替换工具"},
}


def stage_fonts() -> bool:
    """把要分发的字体挑到 build/fonts_pack"""
    if os.path.isdir(FONTS_PACK):
        shutil.rmtree(FONTS_PACK, ignore_errors=True)
    os.makedirs(FONTS_PACK, exist_ok=True)

    missing = [f for f in FONT_FILES if not os.path.exists(os.path.join(FONTS_SRC, f))]
    if missing:
        print(f"[错误] fonts 目录缺少文件: {missing}")
        print("       字体可用 python tools/build_fonts.py 生成")
        return False

    for name in FONT_FILES:
        shutil.copy2(os.path.join(FONTS_SRC, name), os.path.join(FONTS_PACK, name))
    total = sum(os.path.getsize(os.path.join(FONTS_PACK, f)) for f in FONT_FILES)
    print(f"[1/2] 字体已就绪: {len(FONT_FILES)} 个文件, {total / 1048576:.1f} MB")
    return True


def build(target: str) -> bool:
    cfg = TARGETS[target]
    exe_path = os.path.join(ROOT, cfg["name"] + ".exe")
    if os.path.exists(exe_path):
        os.remove(exe_path)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconsole", "--onefile",
        "--name", cfg["name"],
        "--add-data", f"{FONTS_PACK};fonts",
        "--paths", ROOT,
        "--distpath", ROOT,
        "--workpath", os.path.join(BUILD_DIR, "tmp_" + target),
        "--specpath", BUILD_DIR,
    ]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(cfg["entry"])

    print(f"\n[2/2] 打包 {target}: {cfg['name']}.exe")
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print("[错误] 打包失败，日志尾部：")
        tail = (result.stdout or "")[-3000:] + (result.stderr or "")[-2000:]
        print(tail)
        return False

    if not os.path.exists(exe_path):
        print(f"[错误] 没有生成 {exe_path}")
        return False
    size = os.path.getsize(exe_path) / 1048576
    print(f"      -> {exe_path}  {size:.1f} MB")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 PDF 工具（完整版 / 精简版）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--lite", action="store_true", help="只打精简版")
    group.add_argument("--full", action="store_true", help="只打完整版")
    parser.add_argument("--keep-build", action="store_true", help="保留 build 目录（调试用）")
    args = parser.parse_args()

    targets = ["lite"] if args.lite else ["full"] if args.full else ["full", "lite"]

    if not stage_fonts():
        return 1

    failed = [t for t in targets if not build(t)]

    if not args.keep_build:
        shutil.rmtree(BUILD_DIR, ignore_errors=True)

    if failed:
        print(f"\n失败的版本: {failed}")
        return 1
    print(f"\n完成，共 {len(targets)} 个版本。自检: 「PDF工具箱.exe」 --selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
