from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pdfplumber

from app.parsing.base import ParseError, clean_text
from app.schema.doctree import (
    DocBlock,
    DocImage,
    DocMeta,
    DocSection,
    DocTable,
    DocTree,
)
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
    # Word 14pt 标题样式在 PDF 中实为 13.9pt（对 11pt 正文仅 +2.9），
    # 公文三号 16 对四号 14 仅 +2：阈值按这两档真实落差取
    if size >= body_size + 2.0:
        return 1
    if size >= body_size + 1.0:
        return 2
    if _BOLD_FONT.search(fontname) and _NUM_PREFIX.match(text) and w <= 30:
        return 1
    return None


def _col_widths_from_cells(cells: list) -> list[float] | None:
    """单元格 bbox 的 x 边界聚类 → 归一化列宽比例；无边界信息返回 None。"""
    if not cells:
        return None
    try:
        edges = sorted({round(float(c[0]), 1) for c in cells}
                       | {round(float(c[2]), 1) for c in cells})
    except (TypeError, ValueError, IndexError):
        return None
    if len(edges) < 3:  # 至少两列才有列宽可言
        return None
    ws = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
    if sum(ws) <= 0:
        return None
    return [w / sum(ws) for w in ws]


def _to_doctable(grid: list[list[str]], table_id: str, section_id: str,
                 caption: str | None, warnings: list[str],
                 col_widths: list[float] | None = None) -> DocTable:
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
    if col_widths and len(col_widths) != n_cols:
        col_widths = None  # 聚类列数与 grid 不符（罕见），放弃比例
    return DocTable(table_id=table_id, section_id=section_id,
                    n_rows=len(grid), n_cols=n_cols,
                    header=header, rows=rows, caption=caption,
                    col_widths=col_widths)


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
            tables.append({"top": t.bbox[1], "bottom": t.bbox[3], "grid": grid,
                           "col_widths": _col_widths_from_cells(getattr(t, "cells", None))})

    def in_table(top: float, bottom: float) -> bool:
        return any(top >= t["top"] - 1 and bottom <= t["bottom"] + 1
                   for t in tables)

    def _image_events() -> list[dict]:
        try:
            imgs = page.images or []
        except Exception as e:
            warnings.append(f"第 {pno} 页图片提取失败：{e}")
            return []
        out = []
        for im in imgs:
            try:
                x0, top, x1, bottom = (float(im["x0"]), float(im["top"]),
                                       float(im["x1"]), float(im["bottom"]))
            except (KeyError, TypeError, ValueError):
                continue
            if x1 - x0 < 15 or bottom - top < 15:  # 图标/装饰性小图
                continue
            if in_table(top, bottom):  # 表区域内的图防重复计入
                continue
            out.append({"t": "image", "top": top,
                        "bbox": [x0, top, x1, bottom]})
        return out

    # 词 → 行（top 容差随字号放大：大字号标题里混排字体（如 Word 回退字体）
    # 单字 top 可差 3-4pt，固定 2.5 会把标题拆成两行）
    words.sort(key=lambda w: (w["top"], w["x0"]))
    raw_lines: list[dict] = []
    for w in words:
        tol = max(2.5, 0.4 * float(w.get("size") or 0))
        if raw_lines and abs(w["top"] - raw_lines[-1]["top"]) <= tol:
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
        events.append({"t": "table", "top": t["top"], "grid": t["grid"],
                       "col_widths": t.get("col_widths")})
    events.extend(_image_events())
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

    # 扫描件拒收（行文本 + 表格单元格都计入；图片无文本不计）
    total_chars = sum(
        len(e["text"]) if e["t"] == "line"
        else sum(len(c) for row in e["grid"] for c in row) if e["t"] == "table"
        else 0
        for events in pages for e in events)
    if total_chars < 30 or total_chars < _MIN_CHARS_PER_PAGE * n_pages:
        raise ParseError(
            f"疑似扫描件：{n_pages} 页仅提取到 {total_chars} 字符"
            f"（需文字型 PDF，暂不支持 OCR）")

    tables: list[DocTable] = []
    images: list[DocImage] = []
    root = DocSection(section_id="sec-0000", level=0, title=path.stem or "正文")
    stack: list[DocSection] = [root]
    current = root

    blk_n = sec_n = tbl_n = img_n = 0
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

    # 首页首行的大字号行作文档题名（不重复成章节）
    title_taken = False
    first_line_seen = False

    for pno, events in enumerate(pages, 1):
        for e in events:
            if e["t"] == "image":
                flush_para()
                img_n += 1
                image_id = f"img-{img_n:03d}"
                images.append(DocImage(image_id=image_id,
                                       section_id=current.section_id,
                                       page=pno, bbox=e["bbox"]))
                blk_n += 1
                current.blocks.append(DocBlock(block_id=f"blk-{blk_n:04d}",
                                               kind="image", image_id=image_id))
                continue
            if e["t"] == "table":
                flush_para()
                tbl_n += 1
                table_id = f"tbl-{tbl_n:03d}"
                caption = last_para if _CAPTION.match(last_para) else None
                t = _to_doctable(e["grid"], table_id, current.section_id,
                                 caption, warnings,
                                 col_widths=e.get("col_widths"))
                tables.append(t)
                blk_n += 1
                current.blocks.append(DocBlock(block_id=f"blk-{blk_n:04d}",
                                               kind="table", table_id=table_id))
                parts.append(t.flat_text())
                last_para = ""
                continue

            text = e["text"]
            size, fontname = e["size"], e["fontname"]

            if not title_taken and not first_line_seen and pno == 1 \
                    and size >= body_size + 3.5:
                root.title = text
                parts.append(text)
                title_taken = True
                first_line_seen = True
                continue
            first_line_seen = True

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
        images=images,
    )
