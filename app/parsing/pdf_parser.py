from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pdfplumber

from app.parsing.base import ParseError, clean_text, image_size_class
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
_PT_PER_CM = 28.3465

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


# ---- 表格结构重建：cell bbox 边界聚类 → 真实网格（合并/列宽/行高）----
# 双线/三线边框的线间距实测 5.0-7.1pt（绩效考核表类 PDF），真实最窄列 ≥15.6pt：
# 先按跨度 ≤6pt 聚类合并双线，再删 <12pt 的残余窄间隙；两档均低于真实列宽。
_EDGE_TOL = 6.0     # 聚类容差（pt）：组内跨度 ≤ 此值视为同一物理边界
_THIN_GAP = 12.0    # 聚类后相邻边界间距 < 此值视为幻影（三线边框残余等）
_MAX_GRID_CELLS = 2000


def _axis_clusters(vals: list[float], tol: float) -> tuple[list[float], list[int]]:
    """N 个边界值 → 贪心聚类（组内跨度 ≤ tol 取均值）。

    返回 (簇均值, 逐值簇下标)。下标按回放同一贪心过程得出——相邻簇均值可
    近于 tol，按最近距离匹配会错簇，必须记录归属。
    """
    clusters: list[list[float]] = []
    assign = [0] * len(vals)
    for i in sorted(range(len(vals)), key=lambda k: vals[k]):
        v = vals[i]
        if clusters and v - clusters[-1][0] <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
        assign[i] = len(clusters) - 1
    return [sum(g) / len(g) for g in clusters], assign


def _drop_thin(means: list[float], thin: float) -> tuple[list[int], list[float]]:
    """删除与近邻间隙 < thin 的簇边界（保首尾），返回 (旧簇→新簇下标, 保留簇均值)。

    删掉的簇并入最近保留簇：边界两侧 cell 边同映射到同一新下标，无匹配歧义。
    """
    keep = [True] * len(means)
    while True:
        idx = [i for i, k in enumerate(keep) if k]
        drop = None
        for a, b in zip(idx, idx[1:]):
            if means[b] - means[a] < thin:
                drop = a if b == idx[-1] else b  # 保首尾：末间隙删左簇
                break
        if drop is None:
            break
        keep[drop] = False
    kept = [i for i, k in enumerate(keep) if k]
    new_of = {old: n for n, old in enumerate(kept)}
    remap = []
    for i in range(len(means)):
        if keep[i]:
            remap.append(new_of[i])
        else:
            nb = min(kept, key=lambda j: abs(means[j] - means[i]))
            remap.append(new_of[nb])
    return remap, [means[i] for i in kept]


def _reconstruct_table(page, t) -> tuple | None:
    """cell bbox → (grid, merges, col_widths, row_heights)；重建失败返回 None。

    - 边界聚类（双线合一）+ 残余窄间隙删除 → 真实行列边界
    - 每格由 bbox 覆盖的边界区间算 (r0, c0, rowspan, colspan)，span 文本复制
      到全部覆盖位——与 docx 解析的 python-docx 展开语义一致（C6 候选去重兼容）
    - merges 仅记左上角；col_widths 归一化比例；row_heights 为 pt 绝对值
    """
    cells: list[tuple[float, float, float, float]] = []
    for c in (getattr(t, "cells", None) or []):
        if not c or len(c) < 4:
            continue
        try:
            cells.append((float(c[0]), float(c[1]), float(c[2]), float(c[3])))
        except (TypeError, ValueError, IndexError):
            continue
    if not cells:
        return None
    n = len(cells)
    xm, xa = _axis_clusters([c[0] for c in cells] + [c[2] for c in cells],
                            _EDGE_TOL)
    ym, ya = _axis_clusters([c[1] for c in cells] + [c[3] for c in cells],
                            _EDGE_TOL)
    xmap, x_kept = _drop_thin(xm, _THIN_GAP)
    ymap, y_kept = _drop_thin(ym, _THIN_GAP)
    n_cols, n_rows = len(x_kept) - 1, len(y_kept) - 1
    if n_cols < 1 or n_rows < 1 or n_rows * n_cols > _MAX_GRID_CELLS:
        return None
    grid = [[""] * n_cols for _ in range(n_rows)]
    merges: list[list[int]] = []
    for i, (x0, top, x1, bottom) in enumerate(cells):
        c0, c1 = xmap[xa[i]], xmap[xa[n + i]]
        r0, r1 = ymap[ya[i]], ymap[ya[n + i]]
        if c1 <= c0 or r1 <= r0:
            continue  # 双线夹缝等退化格
        text = _cell_text(page, (x0, top, x1, bottom))
        for ri in range(r0, r1):
            for ci in range(c0, c1):
                grid[ri][ci] = text
        if r1 - r0 > 1 or c1 - c0 > 1:
            merges.append([r0, c0, r1 - r0, c1 - c0])
    if not any(any(row) for row in grid):
        return None
    total_w = x_kept[-1] - x_kept[0]
    if total_w <= 0:
        return None
    col_widths = [(b - a) / total_w for a, b in zip(x_kept, x_kept[1:])]
    row_heights = [b - a for a, b in zip(y_kept, y_kept[1:])]
    return grid, merges, col_widths, row_heights


def _cell_text(page, bbox) -> str:
    try:
        pb = page.bbox  # (x0, top, x1, bottom)
        clipped = (max(bbox[0], pb[0]), max(bbox[1], pb[1]),
                   min(bbox[2], pb[2]), min(bbox[3], pb[3]))
        text = page.crop(clipped).extract_text() or ""
    except Exception:
        return ""
    out = ""
    for ln in (clean_text(s) for s in text.splitlines()):
        if ln:
            out = _join(out, ln) if out else ln
    return out


def _to_doctable(grid: list[list[str]], table_id: str, section_id: str,
                 caption: str | None, warnings: list[str],
                 col_widths: list[float] | None = None,
                 merges: list[list[int]] | None = None,
                 row_heights: list[float] | None = None) -> DocTable:
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
    if merges:
        # 合并坐标（含表头行的网格坐标）需落在 grid 内，越界/退化项丢弃
        merges = [m for m in merges
                  if len(m) == 4 and m[0] >= 0 and m[1] >= 0
                  and m[0] + m[2] <= len(grid) and m[1] + m[3] <= n_cols
                  and (m[2] > 1 or m[3] > 1)] or None
    if row_heights and len(row_heights) != len(grid):
        row_heights = None
    return DocTable(table_id=table_id, section_id=section_id,
                    n_rows=len(grid), n_cols=n_cols,
                    header=header, rows=rows, caption=caption,
                    col_widths=col_widths, merges=merges,
                    row_heights=row_heights)


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
        rec = _reconstruct_table(page, t)
        if rec is not None:
            grid, merges, col_widths, row_heights = rec
            # 合并表的空行可能是 vMerge 覆盖行，过滤会错位合并坐标——保留
        else:
            grid = t.extract() or []
            grid = [[clean_text(c) if c is not None else "" for c in row]
                    for row in grid]
            grid = [row for row in grid if any(row)]
            merges = col_widths = row_heights = None
        if grid:
            tables.append({"top": t.bbox[1], "bottom": t.bbox[3], "grid": grid,
                           "col_widths": col_widths, "merges": merges,
                           "row_heights": row_heights})

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
            if in_table(top, bottom):  # 表区域内的图随表格重建，不单独复用
                continue
            w_cm, h_cm = (x1 - x0) / _PT_PER_CM, (bottom - top) / _PT_PER_CM
            size = image_size_class(w_cm, h_cm)
            if size == "portrait":
                warnings.append(
                    f"第 {pno} 页疑似证件照（{w_cm:.1f}×{h_cm:.1f}cm），已跳过不搬运")
                continue
            if size == "icon":  # 图标/装饰性小图
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
                       "col_widths": t.get("col_widths"),
                       "merges": t.get("merges"),
                       "row_heights": t.get("row_heights")})
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
                                 col_widths=e.get("col_widths"),
                                 merges=e.get("merges"),
                                 row_heights=e.get("row_heights"))
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
