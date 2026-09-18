from __future__ import annotations

from pathlib import Path

import pytest

from app.parsing.base import ParseError
from app.parsing.pdf_parser import parse_pdf


def _text_pdf(path: Path, lines: list[tuple[str, float]]) -> Path:
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 72.0
    for text, size in lines:
        page.insert_text((72, y), text, fontname="china-s", fontsize=size)
        y += size + 14
    doc.save(str(path))
    doc.close()
    return path


def _table_pdf(path: Path) -> Path:
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 72), "渠道统计", fontname="china-s", fontsize=16)
    page.insert_text((72, 92), "下表为本季度渠道数据。", fontname="china-s", fontsize=11)

    xs = [72, 172, 272, 372, 472]
    ys = [110, 150, 190, 230]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]), width=1)
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y), width=1)
    cells = [["渠道", "数量", "占比", "备注"],
             ["直销", "56", "43.8%", "重点"],
             ["渠道", "72", "56.2%", "稳定"]]
    for r, row in enumerate(cells):
        for c, v in enumerate(row):
            page.insert_text((xs[c] + 6, ys[r + 1] - 12), v,
                             fontname="china-s", fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def test_parse_pdf_structure(tmp_path):
    p = _text_pdf(tmp_path / "报告.pdf", [
        ("关于市场工作的报告", 20),
        ("一、总体情况", 16),
        ("本季度新增客户 128 家。", 11),
        ("续约率达到 91%。", 11),
    ])
    tree = parse_pdf(p)

    assert tree.meta.source_format == "pdf"
    assert tree.meta.source_name == "报告.pdf"
    assert tree.sections[0].title == "关于市场工作的报告"  # 大字号首页行 → 题名

    l1 = [s for s in tree.sections[0].walk() if s.level == 1]
    assert [s.title for s in l1] == ["一、总体情况"]
    paras = [b.text for b in l1[0].blocks]
    assert paras == ["本季度新增客户 128 家。", "续约率达到 91%。"]
    for needle in ("128", "91%", "关于市场工作的报告"):
        assert needle in tree.full_text


def test_parse_pdf_no_headings_flat(tmp_path):
    p = _text_pdf(tmp_path / "flat.pdf", [
        ("尊敬的公司领导：", 11),
        ("您好！我于 2023 年 3 月入职现岗位，现申请调整至市场部工作。", 11),
        ("恳请领导审批为盼。", 11),
    ])
    tree = parse_pdf(p)
    root = tree.sections[0]
    assert not root.subsections
    texts = [b.text for b in root.blocks]
    assert "尊敬的公司领导：" in texts
    assert any("2023" in t for t in texts)


def test_parse_pdf_list_items(tmp_path):
    p = _text_pdf(tmp_path / "list.pdf", [
        ("重点工作安排", 20),
        ("● 完成需求调研并输出分析报告", 11),
        ("● 上线新版本并回收全量数据", 11),
        ("● 组织复盘会议形成结论", 11),
    ])
    tree = parse_pdf(p)
    kinds = [(b.kind, b.text) for b in tree.sections[0].blocks]
    assert ("list_item", "● 完成需求调研并输出分析报告") in kinds
    assert ("list_item", "● 上线新版本并回收全量数据") in kinds
    assert ("list_item", "● 组织复盘会议形成结论") in kinds


def test_parse_pdf_table_verbatim(tmp_path):
    p = _table_pdf(tmp_path / "表格.pdf")
    tree = parse_pdf(p)

    assert len(tree.tables) == 1
    t = tree.tables[0]
    assert t.header == ["渠道", "数量", "占比", "备注"]
    assert t.rows == [["直销", "56", "43.8%", "重点"], ["渠道", "72", "56.2%", "稳定"]]
    # 表格内容不重复出现在正文块里
    for b in tree.sections[0].walk():
        for blk in b.blocks:
            if blk.kind != "table":
                assert "43.8%" not in blk.text
    # full_text 覆盖表格（数字溯源基准）
    assert "43.8%" in tree.full_text and "56.2%" in tree.full_text
    assert {t.table_id} <= tree.known_refs()


def test_parse_pdf_scanned_rejected(tmp_path):
    import fitz

    p = tmp_path / "扫描件.pdf"
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.new_page(width=595, height=842)
    doc.save(str(p))
    doc.close()

    with pytest.raises(ParseError, match="扫描件"):
        parse_pdf(p)


def test_parse_pdf_missing_file(tmp_path):
    with pytest.raises(ParseError):
        parse_pdf(tmp_path / "nope.pdf")
