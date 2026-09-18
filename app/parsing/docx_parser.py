from __future__ import annotations

import re
from pathlib import Path

from docx import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.parsing.base import ParseError, clean_text
from app.schema.doctree import DocBlock, DocMeta, DocSection, DocTable, DocTree
from app.schema.textlen import text_weight

_HEADING = re.compile(r"^(?:Heading|标题)\s*(\d)$", re.IGNORECASE)
_CAPTION = re.compile(r"^表\s*\d")
_LIST_STYLE = re.compile(r"^(?:List|列表)", re.IGNORECASE)


def _heading_level(p: Paragraph) -> int | None:
    pPr = p._p.pPr
    if pPr is not None:
        ol = pPr.find(qn("w:outlineLvl"))
        if ol is not None and ol.get(qn("w:val")) is not None:
            v = int(ol.get(qn("w:val")))
            if 0 <= v <= 8:
                return v + 1
    m = _HEADING.match(p.style.name or "")
    return int(m.group(1)) if m else None


def _is_list_item(p: Paragraph) -> bool:
    # numPr 可能在段落直接格式，也可能在样式定义里（如 List Bullet）
    pPr = p._p.pPr
    if pPr is not None and pPr.find(qn("w:numPr")) is not None:
        return True
    style = p.style
    if _LIST_STYLE.match(style.name or ""):
        return True
    spPr = style.element.find(qn("w:pPr"))
    return spPr is not None and spPr.find(qn("w:numPr")) is not None


def _iter_blocks(doc) -> "object":
    """按文档顺序产出 Paragraph / Table（doc.paragraphs 与 doc.tables 会丢失交错关系）。"""
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _extract_table(tbl: Table, table_id: str, section_id: str, caption: str | None,
                   warnings: list[str]) -> DocTable:
    grid = [[clean_text(c.text) for c in row.cells] for row in tbl.rows]
    if not grid or not grid[0]:
        warnings.append(f"{table_id}：空表格，已跳过内容")
        return DocTable(table_id=table_id, section_id=section_id, n_rows=0, n_cols=0, caption=caption)

    widths = {len(r) for r in grid}
    n_cols = max(widths)
    if len(widths) > 1:
        warnings.append(f"{table_id}：行宽不齐（{sorted(widths)}），已右侧补空对齐")
        grid = [r + [""] * (n_cols - len(r)) for r in grid]

    first = grid[0]
    if len(grid) >= 2 and all(c and text_weight(c) <= 30 for c in first):
        header, rows = first, grid[1:]
    else:
        header, rows = [], grid
        warnings.append(f"{table_id}：未识别出表头，整表按数据行处理")

    xml = tbl._tbl.xml
    if "vMerge" in xml or "gridSpan" in xml:
        warnings.append(f"{table_id}：含合并单元格，按重复值展开（单元格值仍为 verbatim）")

    return DocTable(
        table_id=table_id, section_id=section_id,
        n_rows=len(grid), n_cols=n_cols,
        header=header, rows=rows, caption=caption,
    )


def parse_docx(path: Path) -> DocTree:
    path = Path(path)
    if not path.exists():
        raise ParseError(f"文件不存在：{path}")
    try:
        doc = DocxDocument(str(path))
    except Exception as e:
        raise ParseError(f"docx 打开失败：{e}") from e

    warnings: list[str] = []
    tables: list[DocTable] = []
    flat_sections: list[DocSection] = []

    root = DocSection(section_id="sec-0000", level=0,
                      title=clean_text(doc.core_properties.title or "") or path.stem or "正文")
    stack: list[DocSection] = [root]
    current = root

    blk_n = sec_n = tbl_n = 0
    parts: list[str] = []
    last_para = ""

    for item in _iter_blocks(doc):
        if isinstance(item, Paragraph):
            text = clean_text(item.text)
            style = item.style.name or ""
            if not text:
                continue
            if style in ("Title", "标题") and root.title in ("", path.stem, "正文"):
                root.title = text
                parts.append(text)
                continue
            level = _heading_level(item)
            if level is not None:
                sec_n += 1
                while len(stack) > level:
                    stack.pop()
                sec = DocSection(section_id=f"sec-{sec_n:04d}", level=level, title=text)
                stack[-1].subsections.append(sec)
                flat_sections.append(sec)
                stack.append(sec)
                current = sec
                parts.append(text)
                last_para = text
                continue
            blk_n += 1
            kind = "list_item" if _is_list_item(item) else "para"
            current.blocks.append(DocBlock(block_id=f"blk-{blk_n:04d}", kind=kind, text=text))
            parts.append(text)
            last_para = text
        else:
            tbl_n += 1
            table_id = f"tbl-{tbl_n:03d}"
            caption = last_para if _CAPTION.match(last_para) else None
            t = _extract_table(item, table_id, current.section_id, caption, warnings)
            tables.append(t)
            blk_n += 1
            current.blocks.append(DocBlock(block_id=f"blk-{blk_n:04d}", kind="table", table_id=table_id))
            parts.append(t.flat_text())
            last_para = ""

    full_text = "\n".join(parts)
    if not full_text:
        warnings.append("文档无文本内容")

    return DocTree(
        meta=DocMeta(source_format="docx", source_name=path.name,
                     n_chars=len(full_text), parse_warnings=warnings),
        sections=[root],
        tables=tables,
        full_text=full_text,
    )
