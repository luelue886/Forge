"""生成简历 golden 样例 examples/resume_sample.docx：表格式简历。

C9 能力全要素：
- label-value 行（词汇字段：姓名/性别/民族/籍贯/毕业院校/邮箱…）→ 值格虚构改写
- 表格内嵌 2.8×3.5cm 证件照（vMerge 四行）→ 渲染期 strip，输出无照片
- 纯数字值（联系电话）→ 照搬，不进 LLM
- 自我评价/工作经历长格（≥12 当量带数字）→ 措辞改写 + ⟦N⟧ 数字保护
- 合并结构：证件照列 vMerge + 两行 gridSpan → 排版指纹断言

用法：.venv/Scripts/python.exe scripts/make_resume_sample.py
"""

from __future__ import annotations

import io
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.shared import Cm

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "examples" / "resume_sample.docx"


def _portrait_png() -> bytes:
    """证件照占位：灰底 + 头肩剪影（PIL 绘制，仅测试尺寸分类用）。"""
    from PIL import Image, ImageDraw

    w, h = 112, 140  # 2.8×3.5cm @ 40dpi
    img = Image.new("RGB", (w, h), (230, 230, 230))
    d = ImageDraw.Draw(img)
    d.ellipse([w // 2 - 28, 22, w // 2 + 28, 78], fill=(160, 160, 165))
    d.polygon([(w // 2 - 42, 132), (w // 2 - 30, 92), (w // 2 + 30, 92),
               (w // 2 + 42, 132)], fill=(150, 150, 155))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> Path:
    doc = Document()
    doc.add_heading("个人简历", 0)

    t = doc.add_table(rows=6, cols=5)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    for i, w in enumerate([Cm(2.2), Cm(4.2), Cm(2.2), Cm(4.2), Cm(2.8)]):
        t.columns[i].width = w
        for cell in t.columns[i].cells:
            cell.width = w

    # 合并：证件照列纵向 4 行；自我评价/工作经历值区横向 4 列
    t.cell(0, 4).merge(t.cell(3, 4))
    t.cell(4, 1).merge(t.cell(4, 4))
    t.cell(5, 1).merge(t.cell(5, 4))

    rows = [
        ["姓 名", "张三", "性 别", "男"],
        ["民 族", "汉族", "出生年月", "1990 年 3 月"],
        ["籍 贯", "浙江杭州", "毕业院校", "浙江工商大学 2012 届"],
        ["联系电话", "13800001234", "电子邮箱", "zhangsan1990@163.com"],
        ["自我评价",
         "本人做事细致耐心，具备 3 年财务核算经验，累计处理会计凭证 1200 余张，"
         "熟悉税务申报与发票管理全流程"],
        ["工作经历",
         "曾任职制造企业会计助理岗位，负责费用报销单据审核与日常账务处理，"
         "月均审核单据 260 张，协助整理年度审计资料 18 份"],
    ]
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            if text:
                t.cell(r, c).text = text

    t.cell(0, 4).paragraphs[0].add_run().add_picture(
        io.BytesIO(_portrait_png()), width=Cm(2.8), height=Cm(3.5))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(OUT))
    print(f"生成 {OUT}")
    return OUT


if __name__ == "__main__":
    main()
