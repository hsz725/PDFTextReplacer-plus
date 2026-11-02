# 字体说明 / Fonts

本目录下的中文字体均为**开源可商用**字体，授权为 SIL Open Font License 1.1
（允许免费商用、嵌入文档、修改与再分发），许可证全文见 `OFL.txt`。

| 文件 | 字体 | 风格 | 来源 |
|---|---|---|---|
| `NotoSansSC-Regular.ttf` | Noto Sans SC | 黑体 | Google Fonts / notofonts/noto-cjk |
| `NotoSerifSC-Regular.ttf` | Noto Serif SC | 宋体 | Google Fonts / notofonts/noto-cjk |

两个文件均由 `tools/build_fonts.py` 从官方可变字体（VF）实例化到 wght=400
并按常用字集（ASCII + Latin-1 + GB2312 + 中日韩标点 + 全角符号，共 7370 个码位）
裁剪而来，体积从 17~25MB 降到 2~3MB。原始 VF 文件放在 `_src/` 下，不参与分发。

## 为什么不使用系统自带字体

Windows 自带的宋体(SimSun)、黑体(SimHei)、微软雅黑(MS YaHei)、等线(DengXian)、
楷体(KaiTi) 等，授权归微软及字体代工厂所有，**不能随程序分发**；把它们嵌入
PDF 用于商业文档也存在授权风险。因此本工具只从本目录挑选字体，
不再读取 `C:\Windows\Fonts`。

## 换成其它字体

把任意 `.ttf` / `.otf` 放进本目录即可，程序会优先使用（按原文字体风格匹配
宋体/黑体，再按字形覆盖校验）。若只想用某一种，删掉另一个即可。

## 注意

`LiberationSans-Bold.ttf` 是仓库原有的西文字体（SIL OFL），保留未动。
