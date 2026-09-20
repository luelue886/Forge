"""form+PDF 分支整文档 HTML 生成（JSON→HTML）。

单 HTML 承载整份公文：@page A4 公文版式、标题黑体二号居中、正文仿宋
四号行距 1.5 首行缩进 2em、表格宋体五号（≥7 列降小五）。Word HTML
导入的怪癖按属性兜底：边框打在 th/td 上（表级被丢弃）、表头底纹用
bgcolor 属性、列宽用 col width 属性、行高 tr style 视为最小高度
（atLeast 语义）。竖排标签格（跨 ≥3 行的短 label）逐字 <br> 竖排。

写出方须用 utf-8-sig（Word 嗅探 BOM），meta charset 双保险。
"""

from __future__ import annotations

import html as _html
from typing import Literal

from pydantic import BaseModel

from app.render.docstyle import (
    BODY_LINE_SPACING,
    MARGIN_BOTTOM,
    MARGIN_LEFT,
    MARGIN_RIGHT,
    MARGIN_TOP,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    SIZE_ER,
    SIZE_SI,
    SIZE_WU,
    SIZE_XIAO_WU,
    TABLE_BORDER_COLOR,
    TABLE_HEADER_FILL,
    TITLE_SPACE_AFTER,
    WIDE_TABLE_COLS,
)
from app.schema.tblskeleton import TSkeleton, TVisualTable

_VERTICAL_MIN_ROWS = 3
_VERTICAL_MAX_CHARS = 10


class FormBlock(BaseModel):
    """文档序渲染项：正文段（终稿文本）/ 骨架表 / 内嵌图片。"""

    kind: Literal["para", "table", "image"]
    text: str = ""
    table_index: int | None = None
    b64: str = ""
    width_cm: float = 0.0


def _esc(s: str) -> str:
    return _html.escape(s, quote=False)


def _is_vertical_label(cell) -> bool:
    return (cell.style == "label" and cell.rowspan >= _VERTICAL_MIN_ROWS
            and 2 <= len(cell.content.strip()) <= _VERTICAL_MAX_CHARS)


def _cell_html(cell, content: str) -> str:
    attrs = ""
    if cell.colspan > 1:
        attrs += f' colspan="{cell.colspan}"'
    if cell.rowspan > 1:
        attrs += f' rowspan="{cell.rowspan}"'
    if _is_vertical_label(cell):
        inner = "<br>".join(_esc(ch) for ch in cell.content.strip())
    else:
        inner = _esc(content)
    border = f"border:0.5pt solid #{TABLE_BORDER_COLOR}"
    if cell.style == "header":
        return (f'<th{attrs} bgcolor="#{TABLE_HEADER_FILL}" align="center" '
                f'style="{border};font-weight:bold">{inner}</th>')
    align = "center" if cell.style in ("label", "option") else "left"
    return f'<td{attrs} align="{align}" style="{border}">{inner}</td>'


def _col_widths_for(s: TSkeleton, v: TVisualTable | None) -> list[int]:
    if v is not None and len(v.col_widths) == s.total_cols \
            and sum(v.col_widths) == 100:
        return v.col_widths
    base = [100 // s.total_cols] * s.total_cols
    base[0] += 100 - sum(base)
    return base


def _table_html(s: TSkeleton, v: TVisualTable | None) -> str:
    widths = _col_widths_for(s, v)
    font_size = (SIZE_XIAO_WU.pt if s.total_cols >= WIDE_TABLE_COLS
                 else SIZE_WU.pt)
    cols = "".join(f'<col width="{w}%">' for w in widths)
    caption = ""
    if s.table_title:
        caption = (f'<p style="font-family:宋体;font-size:{SIZE_WU.pt}pt;'
                   f'font-weight:bold;text-align:center;margin:6pt 0 2pt">'
                   f'{_esc(s.table_title)}</p>')
    heights = v.row_heights if v is not None and v.row_heights else []
    occupied: set[tuple[int, int]] = set()
    rows_html: list[str] = []
    for r, row in enumerate(s.rows):
        h_attr = f' style="height:{heights[r]}pt"' if r < len(heights) else ""
        cells_html: list[str] = []
        c = 0
        for cell in row.cells:
            while (r, c) in occupied:
                c += 1
            cells_html.append(_cell_html(cell, cell.content))
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    occupied.add((r + dr, c + dc))
            c += cell.colspan
        rows_html.append(f"<tr{h_attr}>" + "".join(cells_html) + "</tr>")
    return (f'{caption}<table cellspacing="0" cellpadding="2" '
            f'style="font-family:宋体;font-size:{font_size}pt;width:100%;'
            f'border-collapse:collapse;table-layout:fixed">'
            f'<colgroup>{cols}</colgroup>{"".join(rows_html)}</table>')


def build_form_html(title: str, blocks: list[FormBlock],
                    skeletons: list[TSkeleton],
                    visuals: list[TVisualTable]) -> str:
    """整文档 HTML：标题 + 文档序（段/表/图）。blocks 已含终稿文本。"""
    body: list[str] = []
    for b in blocks:
        if b.kind == "para":
            body.append(f'<p style="font-family:仿宋;font-size:{SIZE_SI.pt}pt;'
                        f'line-height:{BODY_LINE_SPACING};text-indent:2em;'
                        f'margin:0">{_esc(b.text)}</p>')
        elif b.kind == "table":
            if b.table_index is None or not 0 <= b.table_index < len(skeletons):
                continue
            v = visuals[b.table_index] if b.table_index < len(visuals) else None
            body.append(_table_html(skeletons[b.table_index], v))
        elif b.kind == "image" and b.b64:
            w = f"width:{b.width_cm}cm;" if b.width_cm > 0 else ""
            body.append(f'<p style="margin:4pt 0;text-align:center">'
                        f'<img src="data:image/png;base64,{b.b64}" style="{w}"></p>')
    head = (
        "<!DOCTYPE html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n<style>\n"
        f"@page {{ size: {PAGE_WIDTH.cm}cm {PAGE_HEIGHT.cm}cm; "
        f"margin: {MARGIN_TOP.cm}cm {MARGIN_RIGHT.cm}cm "
        f"{MARGIN_BOTTOM.cm}cm {MARGIN_LEFT.cm}cm; }}\n"
        f"body {{ font-family: 仿宋, serif; font-size: {SIZE_SI.pt}pt; }}\n"
        "</style>\n</head>\n<body>\n"
        f'<h1 style="font-family:黑体;font-size:{SIZE_ER.pt}pt;font-weight:bold;'
        f'text-align:center;margin:0 0 {TITLE_SPACE_AFTER.pt}pt">'
        f"{_esc(title)}</h1>\n"
    )
    return head + "\n".join(body) + "\n</body>\n</html>\n"
