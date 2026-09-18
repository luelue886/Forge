from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pdfplumber

from app.parsing.base import ParseError, clean_text
from app.schema.doctree import DocBlock, DocMeta, DocSection, DocTable, DocTree
from app.schema.textlen import text_weight

_CAPTION = re.compile(r"^表\s*\d")
_SENT_END = "。！？；：!?;:"
_BULLET = re.compile(r"^[•·●○◆▪–—\-]\s*")
_NUM_PREFIX = re.compile(
    r"^(?:[一二三四五六七八九十百]+、|（[一二三四五六七八九十百]+）|\d+[.、)]|第[一二三四五六七八九十百\d]+[章节部分条])")
_BOLD_FONT = re.compile(r"(?i)bold|simhei|黑体")

_MIN_CHARS_PER_PAGE = 20  # 全文平均每页低于该字数视为扫描件

# CJK 及全角符号（含全角标点）——两侧均命中时直连，否则补空格还原词边界
_CJK_BOUND = re.compile("[　-〿一-鿿＀-￯]")


def _join(a: str, b: str) -> str:
    """词 → 行拼接：CJK 相邻直连（还原紧排/两端对齐），其余补单空格。"""
    if not a or not b:
        return a + b
    if _CJK_BOUND.match(a[-1]) and _CJK_BOUND.match(b[0]):
        return a + b
    return a + " " + b


def _heading_level(text: str, size: float, fontname: str,
                   body_size: float) -> int | None:
    w = text_weight(text)
    if w > 40 or len(text) > 60:
        return None
    if size >= body_size + 3.0:
        return 1
    if size >= body_size + 1.5:
        return 2
    if _BOLD_FONT.search(fontname) and _NUM_PREFIX.match(text) and w <= 30:
        return 1
    return None


def _to_doctable(grid: list[list[str]], table_id: str, section_id: str,
                 caption: str | None, warnings: list[str]) -> DocTable:
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
    return DocTable(table_id=table_id, section_id=section_id,
                    n_rows=len(grid), n_cols=n_cols,
                    header=header, rows=rows, caption=caption)


def _collect_page(page, pno: int, warnings: list[str]) -> list[dict]:
    """一页 → 事件流（行文本 / 表格），按 top 排序；表格区域内文本剔除防重复。

    pdfplumber 0.11 的 extract_text_lines 不透传 size/fontname，故用
    extract_words(extra_attrs) 取词再按 top 聚合成行。
    """
    try:
        words = page.extract_words(extra_attrs=["size", "fontname"]) or []
    except Exception as e:
        warnings.append(f"第 {pno} 页文本提取失败：{e}")
        words = []
    try:
        found = page.find_tables()
    except Exception as e:
        warnings.append(f"第 {pno} 页表格识别失败（该页降级为纯文本）：{e}")
        found = []

    tables = []
    for t in found:
        grid = t.extract() or []
        grid = [[clean_text(c) if c is not None else "" for c in row] for row in grid]
        grid = [row for row in grid if any(row)]
        if grid:
            tables.append({"top": t.bbox[1], "bottom": t.bbox[3], "grid": grid})

    def in_table(top: float, bottom: float) -> bool:
        return any(top >= t["top"] - 1 and bottom <= t["bottom"] + 1
                   for t in tables)

    # 词 → 行（同 top 容差 2.5pt 聚合）
    words.sort(key=lambda w: (w["top"], w["x0"]))
    raw_lines: list[dict] = []
    for w in words:
        if raw_lines and abs(w["top"] - raw_lines[-1]["top"]) <= 2.5:
            ln = raw_lines[-1]
            ln["parts"].append(w)
            ln["top"] = min(ln["top"], w["top"])
            ln["bottom"] = max(ln["bottom"], w["bottom"])
        else:
            raw_lines.append({"parts": [w], "top": w["top"], "bottom": w["bottom"]})

    events: list[dict] = []
    for ln in raw_lines:
        parts = sorted(ln["parts"], key=lambda w: w["x0"])
        text = ""
        for w in parts:
            text = _join(text, w["text"]) if text else w["text"]
        text = clean_text(text)
        if not text or in_table(ln["top"], ln["bottom"]):
            continue
        biggest = max(parts, key=lambda w: float(w.get("size") or 0))
        events.append({"t": "line", "top": ln["top"], "text": text,
                       "size": round(float(biggest.get("size") or 0), 1),
                       "fontname": biggest.get("fontname") or ""})
    for t in tables:
        events.append({"t": "table", "top": t["top"], "grid": t["grid"]})
    events.sort(key=lambda e: e["top"])
    return events


def parse_pdf(path: Path) -> DocTree:
    path = Path(path)
    if not path.exists():
        raise ParseError(f"文件不存在：{path}")
    try:
        pdf = pdfplumber.open(str(path))
    except Exception as e:
        raise ParseError(f"PDF 打开失败：{e}") from e

    warnings: list[str] = []
    with pdf:
        if not pdf.pages:
            raise ParseError("PDF 无页面")
        pages = [_collect_page(p, i, warnings)
                 for i, p in enumerate(pdf.pages, 1)]
    n_pages = len(pages)

    # 正文字号 = 按字符量加权的众数
    sizes: Counter[float] = Counter()
    for events in pages:
        for e in events:
            if e["t"] == "line":
                sizes[e["size"]] += len(e["text"])
    body_size = sizes.most_common(1)[0][0] if sizes else 10.0

    # 扫描件拒收（行文本 + 表格单元格都计入）
    total_chars = sum(
        len(e["text"]) if e["t"] == "line"
        else sum(len(c) for row in e["grid"] for c in row)
        for events in pages for e in events)
    if total_chars < 30 or total_chars < _MIN_CHARS_PER_PAGE * n_pages:
        raise ParseError(
            f"疑似扫描件：{n_pages} 页仅提取到 {total_chars} 字符"
            f"（需文字型 PDF，暂不支持 OCR）")

    tables: list[DocTable] = []
    root = DocSection(section_id="sec-0000", level=0, title=path.stem or "正文")
    stack: list[DocSection] = [root]
    current = root

    blk_n = sec_n = tbl_n = 0
    parts: list[str] = []
    last_para = ""
    para_buf = ""

    def flush_para() -> None:
        nonlocal para_buf, last_para, blk_n
        if not para_buf:
            return
        blk_n += 1
        kind = "list_item" if _BULLET.match(para_buf) else "para"
        current.blocks.append(DocBlock(block_id=f"blk-{blk_n:04d}",
                                       kind=kind, text=para_buf))
        parts.append(para_buf)
        last_para = para_buf
        para_buf = ""

    # 首页顶部的大字号行作文档题名（不重复成章节）
    title_taken = False

    for pno, events in enumerate(pages, 1):
        for e in events:
            if e["t"] == "table":
                flush_para()
                tbl_n += 1
                table_id = f"tbl-{tbl_n:03d}"
                caption = last_para if _CAPTION.match(last_para) else None
                t = _to_doctable(e["grid"], table_id, current.section_id,
                                 caption, warnings)
                tables.append(t)
                blk_n += 1
                current.blocks.append(DocBlock(block_id=f"blk-{blk_n:04d}",
                                               kind="table", table_id=table_id))
                parts.append(t.flat_text())
                last_para = ""
                continue

            text = e["text"]
            size, fontname = e["size"], e["fontname"]

            if not title_taken and pno == 1 and size >= body_size + 3.5:
                root.title = text
                parts.append(text)
                title_taken = True
                continue

            level = _heading_level(text, size, fontname, body_size)
            if level is not None:
                flush_para()
                sec_n += 1
                while len(stack) > level:
                    stack.pop()
                sec = DocSection(section_id=f"sec-{sec_n:04d}", level=level,
                                 title=text)
                stack[-1].subsections.append(sec)
                stack.append(sec)
                current = sec
                parts.append(text)
                last_para = text
                continue

            if _BULLET.match(text) or (_NUM_PREFIX.match(text)
                                       and text.rstrip()[-1:] not in _SENT_END):
                flush_para()
                blk_n += 1
                kind = "list_item" if _BULLET.match(text) else "para"
                current.blocks.append(DocBlock(block_id=f"blk-{blk_n:04d}",
                                               kind=kind, text=text))
                parts.append(text)
                last_para = text
                continue

            para_buf = _join(para_buf, text) if para_buf else text
            if text.rstrip()[-1:] in _SENT_END:
                flush_para()

    flush_para()
    if not root.blocks and not root.subsections:
        warnings.append("PDF 未提取到有效内容")

    full_text = "\n".join(parts)
    return DocTree(
        meta=DocMeta(source_format="pdf", source_name=path.name,
                     n_chars=len(full_text), parse_warnings=warnings),
        sections=[root],
        tables=tables,
        full_text=full_text,
    )
