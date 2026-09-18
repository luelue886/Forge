from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from app.render.docx_render import render_docir_to_docx
from app.schema.docir import (
    ClosingBlock,
    DocIR,
    DocIRMeta,
    DocTitleBlock,
    HeadingBlock,
    ParaBlock,
    SalutationBlock,
    SignatureBlock,
    TableBlock,
)
from app.schema.enums import Genre


def _east(run) -> str:
    return run._element.rPr.rFonts.get(qn("w:eastAsia"))


def _fill(cell) -> str | None:
    shd = cell._tc.tcPr.find(qn("w:shd"))
    return shd.get(qn("w:fill")) if shd is not None else None


def _letter_doc() -> DocIR:
    return DocIR(meta=DocIRMeta(title="调岗申请书", genre=Genre.LETTER), blocks=[
        DocTitleBlock(text="调岗申请书"),
        SalutationBlock(text="尊敬的公司领导："),
        ParaBlock(text="恳请批准调岗。"),
        ClosingBlock(lines=["此致", "敬礼！"]),
        SignatureBlock(signer="申请人：王明", date="2026 年 9 月 18 日"),
    ])


def test_letter_layout(tmp_path: Path):
    out = render_docir_to_docx(_letter_doc(), tmp_path / "letter.docx")
    d = Document(str(out))

    texts = [p.text for p in d.paragraphs]
    assert texts == ["调岗申请书", "尊敬的公司领导：", "恳请批准调岗。",
                     "此致", "敬礼！", "申请人：王明", "2026 年 9 月 18 日"]
    assert len(d.tables) == 0

    title = d.paragraphs[0]
    assert title.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert title.runs[0].font.size == Pt(22)
    assert title.runs[0].font.bold is True
    assert _east(title.runs[0]) == "黑体"

    salutation = d.paragraphs[1]
    assert salutation.paragraph_format.first_line_indent is None  # 顶格
    assert _east(salutation.runs[0]) == "仿宋"

    body = d.paragraphs[2]
    assert body.paragraph_format.first_line_indent == Pt(28)  # 2 字符
    assert body.paragraph_format.line_spacing == 1.5

    closing1, closing2 = d.paragraphs[3], d.paragraphs[4]
    assert closing1.paragraph_format.first_line_indent == Pt(28)
    assert closing2.paragraph_format.first_line_indent is None

    signer, date = d.paragraphs[5], d.paragraphs[6]
    assert signer.alignment == WD_ALIGN_PARAGRAPH.RIGHT
    assert date.alignment == WD_ALIGN_PARAGRAPH.RIGHT


def test_report_headings_and_table_verbatim(tmp_path: Path):
    header = ["项目", "金额（万元）"]
    rows = [["营业收入", "1,234.56"], ["回款金额", "986.20"]]
    doc = DocIR(meta=DocIRMeta(title="运营报告", genre=Genre.REPORT), blocks=[
        DocTitleBlock(text="运营报告"),
        HeadingBlock(level=1, text="一、总体情况"),
        ParaBlock(text="营收 1,234.56 万元。"),
        HeadingBlock(level=2, text="（一）明细"),
        ParaBlock(text="明细见下表。"),
        TableBlock(table_id="tbl-001", header=list(header),
                   rows=[list(r) for r in rows]),
    ])
    out = render_docir_to_docx(doc, tmp_path / "report.docx")
    d = Document(str(out))

    h1 = d.paragraphs[1]
    assert h1.runs[0].font.size == Pt(16)
    assert _east(h1.runs[0]) == "黑体"
    h2 = d.paragraphs[3]
    assert h2.runs[0].font.size == Pt(14)

    t = d.tables[0]
    # 表格 verbatim diff：单元格与源数据逐格一致
    assert [t.cell(0, c).text for c in range(2)] == header
    for i, row in enumerate(rows, start=1):
        assert [t.cell(i, c).text for c in range(2)] == row
    assert _fill(t.cell(0, 0)) == "D9D9D9"   # 表头浅灰
    assert _fill(t.cell(1, 0)) is None       # 数据行 1 无底纹
    assert _fill(t.cell(2, 0)) == "F5F5F5"   # 斑马纹
    assert t.cell(0, 0).paragraphs[0].runs[0].font.bold is True
    assert _east(t.cell(1, 1).paragraphs[0].runs[0]) == "宋体"
    assert t.cell(1, 1).paragraphs[0].runs[0].font.size == Pt(10.5)


def test_table_without_header(tmp_path: Path):
    doc = DocIR(meta=DocIRMeta(title="表", genre=Genre.FORM), blocks=[
        DocTitleBlock(text="表"),
        TableBlock(table_id="tbl-001", header=[],
                   rows=[["a", "b"], ["c", "d"]]),
    ])
    out = render_docir_to_docx(doc, tmp_path / "form.docx")
    d = Document(str(out))
    t = d.tables[0]
    assert len(t.rows) == 2
    assert [t.cell(0, 0).text, t.cell(0, 1).text] == ["a", "b"]
    assert _fill(t.cell(0, 0)) is None  # 无表头 → 无表头底纹


def test_wide_table_shrinks_font(tmp_path: Path):
    header = [f"列{i}" for i in range(1, 9)]  # 8 列 ≥ 7
    doc = DocIR(meta=DocIRMeta(title="宽表", genre=Genre.FORM), blocks=[
        DocTitleBlock(text="宽表"),
        TableBlock(table_id="tbl-001", header=header, rows=[["x"] * 8]),
    ])
    out = render_docir_to_docx(doc, tmp_path / "wide.docx")
    d = Document(str(out))
    assert d.tables[0].cell(1, 0).paragraphs[0].runs[0].font.size == Pt(9)


def test_page_setup_a4_and_margins(tmp_path: Path):
    out = render_docir_to_docx(_letter_doc(), tmp_path / "page.docx")
    sec = Document(str(out)).sections[0]
    # twips 落盘存在 ≤310 EMU 的舍入误差，用容差断言
    assert abs(sec.page_width - Cm(21.0)) < 400
    assert abs(sec.page_height - Cm(29.7)) < 400
    assert abs(sec.top_margin - Cm(2.54)) < 400
    assert abs(sec.left_margin - Cm(3.18)) < 400
    # 页脚有 PAGE 域
    footer_xml = sec.footer.paragraphs[0]._p.xml
    assert "PAGE" in footer_xml


# ---- C3：表格排版保真 ----

def _source_with_merged_table(path: Path) -> Path:
    from docx.shared import Cm as _Cm

    doc = Document()
    doc.add_paragraph("表1：测试")
    t = doc.add_table(rows=2, cols=3)
    for i, w in enumerate((_Cm(6), _Cm(3), _Cm(2))):
        t.columns[i].width = w
    t.cell(0, 0).text = "基本信息"
    t.cell(0, 2).text = "备注"
    t.cell(1, 0).text = "张三"
    t.cell(1, 1).text = "35"
    t.cell(1, 2).text = "该同志负责产线管理工作。"
    t.cell(0, 0).merge(t.cell(0, 1))  # gridSpan=2
    doc.save(str(path))
    return path


def _gridcol_widths(tbl_el) -> list[int]:
    grid = tbl_el.find(qn("w:tblGrid"))
    return [int(gc.get(qn("w:w"))) for gc in grid.findall(qn("w:gridCol"))]


def test_render_copies_source_table_layout(tmp_path: Path):
    src = _source_with_merged_table(tmp_path / "src.docx")
    src_tbl = Document(str(src)).tables[0]._tbl

    doc = DocIR(meta=DocIRMeta(title="信息表", genre=Genre.FORM), blocks=[
        DocTitleBlock(text="信息表"),
        TableBlock(table_id="tbl-001", src_index=0,
                   header=["基本信息", "基本信息", "备注"],
                   rows=[["张三", "35", "该同志负责产线管理工作。"]]),
    ])
    out = render_docir_to_docx(doc, tmp_path / "out.docx", source=src)
    d = Document(str(out))
    assert len(d.tables) == 1
    got = d.tables[0]
    # 列宽与源逐值一致（deepcopy 保真），内容 verbatim
    assert _gridcol_widths(got._tbl) == _gridcol_widths(src_tbl)
    assert got.cell(1, 1).text == "35"


def test_render_source_index_out_of_range_falls_back(tmp_path: Path):
    src = _source_with_merged_table(tmp_path / "src.docx")
    doc = DocIR(meta=DocIRMeta(title="表", genre=Genre.FORM), blocks=[
        DocTitleBlock(text="表"),
        TableBlock(table_id="tbl-001", src_index=99,
                   header=["甲", "乙"], rows=[["1", "2"]]),
    ])
    out = render_docir_to_docx(doc, tmp_path / "out.docx", source=src)
    d = Document(str(out))
    t = d.tables[0]
    assert t.cell(0, 0).text == "甲"      # 规范重建路径
    assert _fill(t.cell(0, 0)) == "D9D9D9"  # 规范表头底纹（拷贝路径没有）


def test_render_table_col_widths_ratio(tmp_path: Path):
    """无源可搬（PDF 源）时按 col_widths 比例设列宽。"""
    doc = DocIR(meta=DocIRMeta(title="表", genre=Genre.FORM), blocks=[
        DocTitleBlock(text="表"),
        TableBlock(table_id="tbl-001", col_widths=[0.5, 0.3, 0.2],
                   header=["甲", "乙", "丙"], rows=[["1", "2", "3"]]),
    ])
    out = render_docir_to_docx(doc, tmp_path / "out.docx")
    t = Document(str(out)).tables[0]
    ws = [tc.tcPr.find(qn("w:tcW")).get(qn("w:w"))
          for tc in t.rows[0]._tr.findall(qn("w:tc"))]
    assert ws and all(w is not None and w.isdigit() for w in ws)
    assert int(ws[0]) > int(ws[1]) > int(ws[2]) > 0
