# -*- coding: utf-8 -*-
"""精简版入口：只有文本替换，没有区域擦除（打包成「PDF文本替换工具.exe」）

和完整版共用同一份源码，只是关掉擦除标签页；以后改 bug 两个版本同时受益。
"""
import os
import sys

# 让 tools/ 下的入口脚本也能 import 到仓库根目录的 gui.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui

gui.LITE_MODE = True
gui.main()
