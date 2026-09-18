"""文档线规范样式常量（黑白灰公文风）。渲染样式绝不进 DocIR schema。"""

from docx.shared import Cm, Pt

# A4 + 公文页边距
PAGE_WIDTH = Cm(21.0)
PAGE_HEIGHT = Cm(29.7)
MARGIN_TOP = Cm(2.54)
MARGIN_BOTTOM = Cm(2.54)
MARGIN_LEFT = Cm(3.18)
MARGIN_RIGHT = Cm(3.18)

# 中文字号（pt）：二号 22 / 三号 16 / 四号 14 / 五号 10.5 / 小五 9
SIZE_ER = Pt(22)
SIZE_SAN = Pt(16)
SIZE_SI = Pt(14)
SIZE_WU = Pt(10.5)
SIZE_XIAO_WU = Pt(9)

FONT_HEI = "黑体"
FONT_FANGSONG = "仿宋"
FONT_SONG = "宋体"
FONT_LATIN = "Times New Roman"  # 数字/字母

# (eastAsia 字体, 字号, 加粗)
STYLE_TITLE = (FONT_HEI, SIZE_ER, True)
STYLE_H1 = (FONT_HEI, SIZE_SAN, False)
STYLE_H2 = (FONT_HEI, SIZE_SI, False)
STYLE_BODY = (FONT_FANGSONG, SIZE_SI, False)
STYLE_TABLE = (FONT_SONG, SIZE_WU, False)

BODY_LINE_SPACING = 1.5
FIRST_LINE_CHARS = 2          # 正文首行缩进（字符）
FIRST_LINE_FALLBACK = Pt(28)  # 四号 14pt × 2

TITLE_SPACE_AFTER = Pt(12)
H1_SPACE_BEFORE = Pt(12)
H1_SPACE_AFTER = Pt(6)
H2_SPACE_BEFORE = Pt(8)
H2_SPACE_AFTER = Pt(4)
PARA_SPACE_AFTER = Pt(0)

# 表格（黑白灰）
TABLE_HEADER_FILL = "D9D9D9"   # 表头浅灰
TABLE_ZEBRA_FILL = "F5F5F5"    # 斑马纹极浅灰
TABLE_BORDER_COLOR = "BFBFBF"  # 细灰边框
TABLE_BORDER_SIZE = 4          # eighths of a pt → 0.5pt
WIDE_TABLE_COLS = 7            # 列数 ≥ 该值缩小为小五号
