from __future__ import annotations

from pathlib import Path

import pytest

from app.render.docx_render import render_docir_to_docx
from app.render.pdf_preview import export_pdf_page_pngs
from app.schema.docir import (
    DocIR,
    DocIRMeta,
    DocTitleBlock,
    HeadingBlock,
    ParaBlock,
    TableBlock,
)
from app.schema.enums import Genre
from app.services.com_export import ComError, export_docx_pdf


def _report_doc() -> DocIR:
    return DocIR(meta=DocIRMeta(title="运营报告", genre=Genre.REPORT), blocks=[
        DocTitleBlock(text="运营报告"),
        HeadingBlock(level=1, text="一、总体情况"),
        ParaBlock(text="营收 1,234.56 万元，同比增长 8%。"),
        TableBlock(table_id="tbl-001", header=["项目", "金额"],
                   rows=[["营业收入", "1,234.56"]]),
    ])


def test_docx_to_pdf_via_word(tmp_path: Path):
    docx = render_docir_to_docx(_report_doc(), tmp_path / "r.docx")
    try:
        pdf = export_docx_pdf(docx, tmp_path / "r.pdf")
    except ComError as e:
        pytest.skip(f"Word COM 不可用：{e}")
    assert pdf.exists() and pdf.stat().st_size > 1000
    import pymupdf
    with pymupdf.open(str(pdf)) as d:
        assert len(d) == 1
        text = d[0].get_text()
    assert "运营报告" in text and "1,234.56" in text  # 表格数字进 PDF


def test_pdf_preview_pngs(tmp_path: Path):
    import pymupdf
    pdf = tmp_path / "two.pdf"
    with pymupdf.open() as d:  # A4 两页
        for _ in range(2):
            d.new_page(width=595, height=842)
        d.save(str(pdf))
    pngs = export_pdf_page_pngs(pdf, tmp_path / "pages", width_px=500)
    assert [p.name for p in pngs] == ["page_01.png", "page_02.png"]
    pm = pymupdf.Pixmap(str(pngs[0]))
    assert pm.width == 500
    assert abs(pm.height - 500 * 842 / 595) < 2
