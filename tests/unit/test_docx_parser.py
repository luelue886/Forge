from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document

from app.parsing.docx_parser import parse_docx
from app.parsing.base import ParseError


def test_basic_structure(basic_docx):
    tree = parse_docx(basic_docx)
    assert tree.meta.source_format == "docx"
    assert tree.meta.source_name == "basic.docx"
    assert tree.meta.n_chars > 0
    assert tree.meta.parse_warnings == []

    root = tree.sections[0]
    assert root.title == "智慧园区平台建设汇报"
    assert [s.title for s in root.subsections] == ["一、项目概述", "二、季度指标"]
    overview, metrics = root.subsections

    kinds = [b.kind for b in overview.blocks]
    assert kinds == ["para", "list_item", "list_item"]
    assert overview.blocks[0].text == "本项目覆盖 12 个园区，接入设备 8,600 台。"

    # 表格块 + 收尾段落在第二个 section
    assert [b.kind for b in metrics.blocks] == ["para", "table", "para"]
    assert metrics.blocks[1].table_id == "tbl-001"


def test_table_verbatim_and_caption(basic_docx):
    tree = parse_docx(basic_docx)
    t = tree.tables[0]
    assert t.table_id == "tbl-001"
    assert t.header == ["指标", "目标", "实际"]
    assert t.rows == [["园区数", "10", "12"], ["设备数", "8000", "8600"]]
    assert t.n_rows == 3 and t.n_cols == 3
    assert t.caption == "表1：Q3 关键指标"
    assert t.section_id == "sec-0002"
    # full_text 必须包含表格原文（数字溯源基准）
    assert "8600" in tree.full_text


def test_resolve_refs(basic_docx):
    tree = parse_docx(basic_docx)
    refs = tree.known_refs()
    assert {"sec-0000", "sec-0001", "sec-0002", "tbl-001"} <= refs
    assert any(r.startswith("blk-") for r in refs)
    assert "8,600" in tree.resolve(["blk-0001"])
    assert "指标" in tree.resolve(["tbl-001"])
    assert "一、项目概述" in tree.resolve(["sec-0001"])
    assert tree.resolve(["不存在"]) == ""


def test_no_headings(tmp_path):
    doc = Document()
    doc.add_paragraph("第一段，含数字 42。")
    doc.add_paragraph("第二段。")
    p = tmp_path / "flat.docx"
    doc.save(str(p))
    tree = parse_docx(p)
    root = tree.sections[0]
    assert root.subsections == []
    assert [b.kind for b in root.blocks] == ["para", "para"]
    assert "42" in tree.full_text


def test_table_without_header(tmp_path):
    doc = Document()
    doc.add_paragraph("无表头表：")
    t = doc.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text = ""
    t.rows[0].cells[1].text = "数据列"
    t.rows[1].cells[0].text = "甲"
    t.rows[1].cells[1].text = "乙"
    p = tmp_path / "noheader.docx"
    doc.save(str(p))
    tree = parse_docx(p)
    t = tree.tables[0]
    assert t.header == []
    assert len(t.rows) == 2
    assert any("表头" in w for w in tree.meta.parse_warnings)


def test_merged_cells_warning(tmp_path):
    doc = Document()
    doc.add_paragraph("表2：合并示例")
    t = doc.add_table(rows=2, cols=3)
    t.rows[0].cells[0].text = "维度"
    t.rows[0].cells[1].text = "数值"
    t.rows[0].cells[2].text = "备注"
    t.rows[1].cells[0].text = "整体"
    t.rows[1].cells[1].text = "99"
    t.rows[1].cells[2].text = "-"
    t.cell(0, 0).merge(t.cell(0, 1))  # 横向合并表头前两格
    p = tmp_path / "merged.docx"
    doc.save(str(p))
    tree = parse_docx(p)
    assert any("合并单元格" in w for w in tree.meta.parse_warnings)
    # 合并展开为重复值，值本身 verbatim
    assert tree.tables[0].rows == [["整体", "99", "-"]]


def test_missing_file(tmp_path):
    with pytest.raises(ParseError, match="不存在"):
        parse_docx(tmp_path / "nope.docx")


def test_ids_stable_across_parses(basic_docx):
    t1, t2 = parse_docx(basic_docx), parse_docx(basic_docx)
    assert t1.known_refs() == t2.known_refs()
    assert t1.full_text == t2.full_text
