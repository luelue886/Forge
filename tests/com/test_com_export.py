from __future__ import annotations

from pathlib import Path

import pytest

from app.render.renderer import render_to_file
from app.schema.enums import IssueCode
from app.services.com_export import checklist, export_pngs

pytestmark = pytest.mark.com


@pytest.fixture(scope="module")
def rendered(tmp_path_factory, deck_all_types) -> Path:
    out = tmp_path_factory.mktemp("com") / "deck.pptx"
    render_to_file(deck_all_types, "business_blue", out)
    return out


def test_export_pngs_count(rendered, tmp_path):
    pngs = export_pngs(rendered, tmp_path / "pngs")
    assert len(pngs) == 8
    for p in pngs:
        assert p.exists() and p.stat().st_size > 1000


def test_checklist_clean(rendered, deck_all_types):
    issues = checklist(deck_all_types, rendered)
    assert not [i for i in issues if i.code is IssueCode.E_RENDER_MISMATCH], \
        [i.detail for i in issues]


def test_checklist_detects_page_mismatch(rendered, deck_all_types):
    from app.schema.slideir import Deck

    tampered = Deck.model_validate(deck_all_types.model_dump())
    tampered.slides = tampered.slides[:-1]  # 模拟 IR 与 pptx 页数不一致
    issues = checklist(tampered, rendered)
    assert any(i.code is IssueCode.E_RENDER_MISMATCH and "页数" in i.detail for i in issues)


def test_convert_doc_to_docx_roundtrip(basic_docx, tmp_path):
    """真 Word：docx → .doc（wdFormatDocument=0）→ convert_doc_to_docx 还原可解析。"""
    from app.parse_dispatch import parse_source
    from app.services.com_export import convert_doc_to_docx, get_word_com_service

    def _to_doc(svc):
        app = svc._ensure_app()
        doc = app.Documents.Open(str(Path(basic_docx).resolve()), False, True, False)
        try:
            out = tmp_path / "legacy.doc"
            doc.SaveAs2(str(out.resolve()), FileFormat=0)  # wdFormatDocument
            return out
        finally:
            doc.Close(False)

    legacy = get_word_com_service().run(_to_doc)
    converted = tmp_path / "legacy.converted.docx"
    convert_doc_to_docx(legacy, converted)

    tree = parse_source(legacy)  # converted 已在 → 幂等跳过，直接解析
    assert "智慧园区" in tree.full_text
    assert any(".doc" in w for w in tree.meta.parse_warnings)


def test_word_accepts_copied_table_xml(tmp_path):
    """C3 关键风险：deepcopy 跨包的表格 XML Word 必须能正常打开并导 PDF。"""
    from docx import Document as DocxDocument
    from docx.shared import Cm

    from app.render.docx_render import render_docir_to_docx
    from app.schema.docir import DocIR, DocIRMeta, DocTitleBlock, TableBlock
    from app.schema.enums import Genre
    from app.services.com_export import export_docx_pdf

    src = tmp_path / "src.docx"
    d = DocxDocument()
    d.add_paragraph("表1：人员")
    t = d.add_table(rows=3, cols=3)
    for i, w in enumerate((Cm(6), Cm(3), Cm(2))):
        t.columns[i].width = w
    t.cell(0, 0).text = "基本信息"
    t.cell(0, 2).text = "备注"
    t.cell(1, 0).text = "张三"
    t.cell(1, 1).text = "35"
    t.cell(1, 2).text = "该同志负责产线管理工作。"
    t.cell(2, 0).text = "李四"
    t.cell(2, 1).text = "42"
    t.cell(2, 2).text = "在岗。"
    t.cell(0, 0).merge(t.cell(0, 1))
    d.save(str(src))

    ir = DocIR(meta=DocIRMeta(title="人员信息表", genre=Genre.FORM), blocks=[
        DocTitleBlock(text="人员信息表"),
        TableBlock(table_id="tbl-001", src_index=0,
                   header=["基本信息", "基本信息", "备注"],
                   rows=[["张三", "35", "该同志负责产线管理工作。"],
                         ["李四", "42", "在岗。"]]),
    ])
    docx = render_docir_to_docx(ir, tmp_path / "out.docx", source=src)
    pdf = export_docx_pdf(docx, tmp_path / "out.pdf")
    assert pdf.exists() and pdf.stat().st_size > 2000

    import pymupdf

    with pymupdf.open(str(pdf)) as doc:
        assert len(doc) >= 1
        assert "张三" in doc[0].get_text()


def test_convert_html_to_docx(tmp_path):
    """T4 关键风险：Word HTML 导入 @page/colgroup/rowspan → docx 保真。"""
    from docx import Document as DocxDocument

    from app.render.form_html import FormBlock, build_form_html
    from app.schema.tblskeleton import TCell, TRow, TSkeleton, TVisualTable
    from app.services.com_export import convert_html_to_docx, export_docx_pdf

    sk = TSkeleton(
        table_title="人员登记表", total_cols=3, src_tables=[], rows=[
            TRow(cells=[TCell(content="维度", style="header"),
                        TCell(content="字段", style="header"),
                        TCell(content="内容", style="header")]),
            TRow(cells=[TCell(content="基本情况", rowspan=2, style="label"),
                        TCell(content="姓名", style="label"),
                        TCell(content="负责产线管理，覆盖 12 条产线")]),
            TRow(cells=[TCell(content="电话", style="label"),
                        TCell(content="13800001111")]),
        ])
    v = TVisualTable(table_index=0, col_widths=[14, 14, 72],
                     row_heights=[28, 40, 24])
    html = build_form_html("人员登记表", [FormBlock(kind="table",
                                                   table_index=0)], [sk], [v])
    html_path = tmp_path / "form.html"
    html_path.write_text(html, encoding="utf-8-sig")
    docx = convert_html_to_docx(html_path, tmp_path / "form.docx")

    d = DocxDocument(str(docx))
    assert len(d.tables) == 1
    xml = d.element.xml
    assert "gridSpan" in xml or "vMerge" in xml
    sec = d.sections[0]
    assert abs(sec.page_width.cm - 21.0) < 0.2
    assert abs(sec.page_height.cm - 29.7) < 0.2
    assert abs(sec.top_margin.cm - 2.54) < 0.2
    assert abs(sec.left_margin.cm - 3.18) < 0.2
    full = "\n".join(p.text for p in d.paragraphs) + "\n" + "\n".join(
        c.text for t in d.tables for r in t.rows for c in r.cells)
    assert "人员登记表" in full and "基本情况" in full
    assert "13800001111" in full and "12" in full
    assert "⟦" not in full

    pdf = export_docx_pdf(docx, tmp_path / "form.pdf")
    import pymupdf

    with pymupdf.open(str(pdf)) as doc:
        text = "".join(page.get_text() for page in doc)
        assert "基本情况" in text
        assert "13800001111" in text and "12" in text
