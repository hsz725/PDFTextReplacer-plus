# -*- coding: utf-8 -*-
"""完整版入口：文本替换 + 区域擦除（打包成「PDF工具箱.exe」）"""
import os
import sys

# 让 tools/ 下的入口脚本也能 import 到仓库根目录的 gui.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui

gui.LITE_MODE = False
gui.main()
