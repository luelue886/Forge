from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import ImageFont

from app.config import get_settings
from app.render import design
from app.render.design import TextSpec
from app.schema.enums import IssueCode, Severity, SlideType
from app.schema.qa import QAIssue
from app.schema.slideir import Deck, SlideIR

PT_TO_PX = 96 / 72
LINE_FACTOR = 1.25  # msyh 行高近似（ascent+descent ≈ 1.25em）
BULLET_INDENT_PX = 24  # 0.25in 悬挂缩进

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,%/+:\-]*|\s+|.")

_fonts: dict[int, ImageFont.FreeTypeFont] = {}


def _font(pt: float) -> ImageFont.FreeTypeFont:
    px = max(1, round(pt * PT_TO_PX))
    if px not in _fonts:
        _fonts[px] = ImageFont.truetype(get_settings().font_path, px)
    return _fonts[px]


def text_px(text: str, pt: float) -> float:
    return _font(pt).getlength(text)


def wrap_lines(text: str, pt: float, max_px: float) -> int:
    """CJK 逐字折行，ASCII 词保持整体。"""
    lines, cur = 1, 0.0
    for tok in _TOKEN.findall(text):
        w = _font(pt).getlength(tok)
        if cur > 0 and cur + w > max_px:
            lines += 1
            cur = 0.0 if not tok.strip() else w
        else:
            cur += w
    return lines


@dataclass
class CapacityItem:
    page_no: int
    name: str
    ratio: float
    detail: str


def _fill_ratio(paras: list[str], spec: TextSpec, margin_v: float = 0.02) -> tuple[float, int, int]:
    usable_w = (spec.w - 2 * spec.inset) * 96
    if spec.bullet:
        usable_w -= BULLET_INDENT_PX
    needed = 0.0
    for i, t in enumerate(paras):
        lines = wrap_lines(t, spec.font_pt, max(usable_w, 10.0))
        needed += lines * spec.font_pt * PT_TO_PX * spec.spacing * LINE_FACTOR
        if i:
            needed += spec.space_before * PT_TO_PX
    avail = spec.h * 96 - 2 * margin_v * 96
    return needed / max(avail, 1.0), int(needed), int(avail)


def _check_table(ir: SlideIR) -> list[CapacityItem]:
    t = ir.table
    if t is None:
        return []
    x, _y, w, h = design.TABLE_REGION
    n_rows = len(t.rows) + (1 if t.header else 0)
    needed = ((design.TABLE_ROW_H_HEADER if t.header else 0) + len(t.rows) * design.TABLE_ROW_H_DATA) * 96
    avail = h * 96
    items = [CapacityItem(ir.page_no, "tbl_frame", needed / avail, f"tbl_frame: 需要 {needed:.0f}px / 可用 {avail:.0f}px")]

    widths = [len(t.header)] + [len(r) for r in t.rows]
    n_cols = max(widths) if widths else 0
    if n_cols:
        col_w = w / n_cols
        cell_spec = TextSpec(0, 0, col_w, design.TABLE_ROW_H_DATA, design.TABLE_FONT_PT, 1.15, inset=0.08)
        cells = list(t.header) + [c for r in t.rows for c in r]
        worst = max(cells, key=lambda c: text_px(c, design.TABLE_FONT_PT)) if cells else ""
        ratio, needed_px, avail_px = _fill_ratio([worst], cell_spec, margin_v=0.03)
        items.append(CapacityItem(ir.page_no, "tbl_cell", ratio, f"tbl_cell: 最长单元格「{worst[:12]}」需要 {needed_px}px / 可用 {avail_px}px"))
    return items


def check_slide(ir: SlideIR) -> list[CapacityItem]:
    layout = design.layout_for(ir)
    tmap = design.texts_for(ir)
    items: list[CapacityItem] = []
    for name, spec in layout.texts.items():
        paras = tmap.get(name)
        if not paras:
            continue
        ratio, needed, avail = _fill_ratio(paras, spec)
        items.append(CapacityItem(ir.page_no, name, ratio, f"{name}: 需要 {needed}px / 可用 {avail}px"))
    if ir.slide_type is SlideType.TABLE:
        items.extend(_check_table(ir))
    return items


def capacity_issues(deck: Deck) -> list[QAIssue]:
    warn = get_settings().capacity_warn
    issues: list[QAIssue] = []
    for ir in deck.slides:
        for item in check_slide(ir):
            if item.ratio > 1.0:
                issues.append(QAIssue(
                    code=IssueCode.E_OVERFLOW, severity=Severity.BLOCKING,
                    page_no=item.page_no, detail=item.detail,
                ))
            elif item.ratio >= warn:
                issues.append(QAIssue(
                    code=IssueCode.W_CAPACITY_90, severity=Severity.COSMETIC,
                    page_no=item.page_no, detail=item.detail,
                ))
    return issues
