from __future__ import annotations

from dataclasses import dataclass, field

from app.schema.enums import SlideType
from app.schema.slideir import SlideIR

# 画布与边距（英寸，16:9）
SLIDE_W = 13.333
SLIDE_H = 7.5
M_L = 0.6
CONTENT_W = SLIDE_W - 2 * M_L

# 表格
TABLE_REGION = (0.70, 1.55, 11.93, 5.00)
TABLE_ROW_H_HEADER = 0.45
TABLE_ROW_H_DATA = 0.42
TABLE_FONT_PT = 12


@dataclass(frozen=True)
class TextSpec:
    x: float
    y: float
    w: float
    h: float
    font_pt: float = 16.0
    spacing: float = 1.3
    bold: bool = False
    align: str = "left"  # left | center | right
    anchor: str = "top"  # top | middle
    bullet: bool = False
    space_before: float = 0.0  # pt，段前间距（首段除外）
    inset: float = 0.05  # 左右内边距（英寸）
    color_role: str = "text"


@dataclass(frozen=True)
class RectSpec:
    x: float
    y: float
    w: float
    h: float
    color_role: str
    line_role: str | None = None
    rounded: bool = False
    name: str = ""


@dataclass(frozen=True)
class SlideLayout:
    texts: dict[str, TextSpec] = field(default_factory=dict)
    rects: list[RectSpec] = field(default_factory=list)
    footer: bool = False
    bg_role: str = "bg"


def _title() -> TextSpec:
    return TextSpec(0.88, 0.40, 11.85, 0.80, 26, 1.15, bold=True, color_role="title")


def _accent() -> RectSpec:
    return RectSpec(0.60, 0.52, 0.10, 0.56, "accent", name="fx_accent")


FOOT_LINE = RectSpec(0.60, 7.06, CONTENT_W, 0.012, "divider", name="fx_footline")
FOOT_L = TextSpec(0.60, 7.12, 8.0, 0.30, 9, 1.1, color_role="footer_text")
FOOT_R = TextSpec(9.33, 7.12, 3.40, 0.30, 9, 1.1, align="right", color_role="footer_text")


def layout_for(ir: SlideIR) -> SlideLayout:
    t = ir.slide_type

    if t in (SlideType.COVER, SlideType.CLOSING):
        texts = {
            "ph_title": TextSpec(1.0, 2.45, 11.33, 1.15, 38, 1.2, bold=True, align="center", color_role="cover_title"),
            "ph_subtitle": TextSpec(1.0, 3.95, 11.33, 0.65, 17, 1.3, align="center", color_role="cover_subtitle"),
        }
        rects = [RectSpec((SLIDE_W - 1.6) / 2, 3.72, 1.6, 0.035, "cover_subtitle", name="fx_line")]
        return SlideLayout(texts, rects, footer=False, bg_role="cover_bg")

    if t is SlideType.SECTION_HEADER:
        texts = {
            "ph_title": TextSpec(1.45, 2.75, 10.5, 1.05, 30, 1.2, bold=True, color_role="title"),
            "ph_subtitle": TextSpec(1.45, 3.95, 10.5, 0.60, 15, 1.3, color_role="muted"),
        }
        rects = [RectSpec(1.05, 2.75, 0.12, 1.05, "accent", name="fx_accent")]
        return SlideLayout(texts, rects, footer=False, bg_role="section_bg")

    if t is SlideType.TOC:
        texts = {
            "ph_title": _title(),
            "ph_body": TextSpec(0.90, 1.60, 11.83, 5.30, 16, 1.6, bullet=True, space_before=10),
        }
        return SlideLayout(texts, [_accent()], footer=True)

    if t is SlideType.TEXT_POINTS:
        has_m = bool(ir.metrics)
        texts = {
            "ph_title": _title(),
            "ph_body": TextSpec(0.90, 1.60, 11.83, 3.30 if has_m else 4.30, 16, 1.35, bullet=True, space_before=10),
        }
        rects = [_accent()]
        if has_m:
            n = len(ir.metrics)
            gap = 0.3
            cw = (CONTENT_W - (n - 1) * gap) / n
            for i in range(n):
                x = M_L + i * (cw + gap)
                texts[f"ph_mv{i+1}"] = TextSpec(x, 5.50, cw, 0.72, 22, 1.15, bold=True, align="center", color_role="accent")
                texts[f"ph_ml{i+1}"] = TextSpec(x, 6.22, cw, 0.45, 11, 1.2, align="center", color_role="muted")
        return SlideLayout(texts, rects, footer=True)

    if t is SlideType.TWO_COLUMN:
        texts = {"ph_title": _title()}
        for i, (x, w) in enumerate([(0.60, 5.85), (7.08, 5.65)]):
            texts[f"ph_c{i+1}_head"] = TextSpec(x, 1.70, w, 0.55, 17, 1.2, bold=True, color_role="column_heading")
            texts[f"ph_c{i+1}_body"] = TextSpec(x, 2.35, w, 4.30, 14, 1.3, bullet=True, space_before=8)
        rects = [_accent(), RectSpec(6.665, 1.75, 0.015, 4.90, "divider", name="fx_divider")]
        return SlideLayout(texts, rects, footer=True)

    if t is SlideType.TABLE:
        return SlideLayout({"ph_title": _title()}, [_accent()], footer=True)

    if t is SlideType.KEY_METRICS:
        texts = {"ph_title": _title()}
        rects = [_accent()]
        n = max(len(ir.metrics), 1)
        gap = 0.45
        cw = min(3.8, (CONTENT_W - (n - 1) * gap) / n)
        total = n * cw + (n - 1) * gap
        x0 = M_L + (CONTENT_W - total) / 2
        for i in range(len(ir.metrics)):
            cx = x0 + i * (cw + gap)
            rects.append(RectSpec(cx, 2.35, cw, 2.30, "card_bg", line_role="card_border", rounded=True, name=f"fx_card{i+1}"))
            texts[f"ph_mv{i+1}"] = TextSpec(cx + 0.15, 2.90, cw - 0.30, 0.95, 34, 1.15, bold=True, align="center", color_role="accent")
            texts[f"ph_ml{i+1}"] = TextSpec(cx + 0.15, 3.90, cw - 0.30, 0.55, 13, 1.2, align="center", color_role="muted")
        if ir.bullets:
            texts["ph_body"] = TextSpec(0.90, 5.15, 11.83, 1.20, 14, 1.3, bullet=True, space_before=8)
        return SlideLayout(texts, rects, footer=True)

    raise ValueError(f"未知页型：{t}")


def texts_for(ir: SlideIR) -> dict[str, list[str]]:
    """shape 名 → 段落文本。renderer 与 capacity 共用同一映射，保证永不失同步。"""
    out: dict[str, list[str]] = {"ph_title": [ir.title]}
    if ir.subtitle is not None and ir.subtitle.strip():
        out["ph_subtitle"] = [ir.subtitle]
    if ir.bullets:
        out["ph_body"] = ir.bullets
    for i, c in enumerate(ir.columns):
        out[f"ph_c{i+1}_head"] = [c.heading]
        out[f"ph_c{i+1}_body"] = c.bullets
    for i, m in enumerate(ir.metrics):
        label = f"{m.label}（{m.unit}）" if m.unit else m.label
        out[f"ph_mv{i+1}"] = [m.value]
        out[f"ph_ml{i+1}"] = [label]
    return out
