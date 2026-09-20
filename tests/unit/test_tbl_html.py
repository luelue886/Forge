from __future__ import annotations

from app.render.form_html import (
    FormBlock,
    _col_widths_for,
    _is_vertical_label,
    build_form_html,
)
from app.schema.tblskeleton import TCell, TRow, TSkeleton, TVisualTable


def _skeleton() -> TSkeleton:
    return TSkeleton(
        table_title="人员信息表", total_cols=3, src_tables=["tbl-001"],
        rows=[
            TRow(cells=[TCell(content="字段", style="header"),
                        TCell(content="内容", style="header"),
                        TCell(content="备注", style="header")]),
            TRow(cells=[TCell(content="姓名", style="label"),
                        TCell(content="张三", style="input"),
                        TCell(content="")]),
            TRow(cells=[TCell(content="出勤情况", rowspan=3, style="label"),
                        TCell(content="正常", style="input"),
                        TCell(content="")]),
            TRow(cells=[TCell(content="正常", style="input"),
                        TCell(content="")]),
            TRow(cells=[TCell(content="正常", style="input"),
                        TCell(content="")]),
        ])


def _visual() -> TVisualTable:
    return TVisualTable(table_index=0, col_widths=[20, 50, 30],
                        row_heights=[28, 24, 80, 24, 24])


# ---- 竖排标签判定 ----

def test_is_vertical_label_rules():
    base = dict(style="label", rowspan=3)
    assert _is_vertical_label(TCell(content="出勤情况", **base))
    assert not _is_vertical_label(TCell(content="勤", **base))          # 单字
    assert not _is_vertical_label(TCell(content="出" * 11, **base))     # 超长
    assert not _is_vertical_label(TCell(content="出勤情况", style="label",
                                        rowspan=2))                    # 行数不足
    assert not _is_vertical_label(TCell(content="出勤情况", style="input",
                                        rowspan=3))                    # 非 label


# ---- 列宽兜底 ----

def test_col_widths_for_equal_split_without_visual():
    s = TSkeleton(table_title="", total_cols=3, rows=[])
    assert _col_widths_for(s, None) == [34, 33, 33]


# ---- 整文档 golden ----

def test_build_form_html_golden():
    html = build_form_html(
        "某单位人员信息表",
        [FormBlock(kind="para", text="本表用于登记人员基本信息。"),
         FormBlock(kind="table", table_index=0),
         FormBlock(kind="image", b64="AAAA", width_cm=5.0)],
        [_skeleton()], [_visual()])
    # 公文版式 @page
    assert ("@page { size: 21.0cm 29.7cm; "
            "margin: 2.54cm 3.18cm 2.54cm 3.18cm; }") in html
    assert '<meta charset="utf-8">' in html
    # 标题：黑体二号居中
    assert ('<h1 style="font-family:黑体;font-size:22.0pt;font-weight:bold;'
            'text-align:center;margin:0 0 12.0pt">某单位人员信息表</h1>') in html
    # 正文段：仿宋四号 行距 1.5 首行缩进 2em
    assert ('<p style="font-family:仿宋;font-size:14.0pt;line-height:1.5;'
            'text-indent:2em;margin:0">本表用于登记人员基本信息。</p>') in html
    # 表题：宋体五号加粗居中
    assert ('<p style="font-family:宋体;font-size:10.5pt;font-weight:bold;'
            'text-align:center;margin:6pt 0 2pt">人员信息表</p>') in html
    # 表格：宋体五号（3 列不降字号）、固定布局
    assert ('<table cellspacing="0" cellpadding="2" '
            'style="font-family:宋体;font-size:10.5pt;width:100%;'
            'border-collapse:collapse;table-layout:fixed">') in html
    # colgroup 整数百分比
    assert ('<colgroup><col width="20%"><col width="50%">'
            '<col width="30%"></colgroup>') in html
    # 行高：首行 28pt
    assert '<tr style="height:28pt">' in html
    # 表头：底纹 + 加粗 + 居中 + 边框在格上
    assert ('<th bgcolor="#D9D9D9" align="center" '
            'style="border:0.5pt solid #BFBFBF;font-weight:bold">字段</th>') in html
    # label 居中 / input 左对齐
    assert '<td align="center" style="border:0.5pt solid #BFBFBF">姓名</td>' in html
    assert ('<td align="left" style="border:0.5pt solid #BFBFBF">'
            '张三</td>') in html
    # 竖排标签：逐字 <br>；rowspan 属性
    assert '<td rowspan="3" align="center" style="border:0.5pt solid #BFBFBF">出<br>勤<br>情<br>况</td>' in html
    # 图片：base64 内嵌 + 宽度
    assert ('<img src="data:image/png;base64,AAAA" '
            'style="width:5.0cm;">') in html


def test_build_form_html_colspan_and_wide_table_font():
    s7 = TSkeleton(table_title="", total_cols=7, rows=[
        TRow(cells=[TCell(content="表头", style="header"),
                    TCell(content="填写区", colspan=6, style="input")]),
    ])
    html = build_form_html("t", [FormBlock(kind="table", table_index=0)],
                           [s7], [])
    # ≥7 列降小五号
    assert "font-size:9.0pt;width:100%" in html
    # colspan 属性
    assert '<td colspan="6" align="left"' in html
    # 无视觉 → 均分列宽（100//7=14，余 2 给首列 → 16,14,…,14）
    assert '<col width="16%">' in html and '<col width="14%">' in html


def test_build_form_html_escapes_text():
    s = TSkeleton(table_title="", total_cols=1, rows=[
        TRow(cells=[TCell(content="a<b&c", style="input")])])
    html = build_form_html("t<x", [FormBlock(kind="table", table_index=0)],
                           [s], [])
    assert "a&lt;b&amp;c" in html
    assert "t<x" not in html and "t&lt;x" in html


def test_build_form_html_skips_bad_table_index():
    html = build_form_html("t", [FormBlock(kind="table", table_index=5)],
                           [_skeleton()], [_visual()])
    assert "<table" not in html
