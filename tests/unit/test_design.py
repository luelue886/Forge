from __future__ import annotations

from app.render import design
from app.schema.enums import SlideType
from app.schema.slideir import BLOCK_WHITELIST, SlideIR


def _minimal(slide_type: SlideType) -> SlideIR:
    kw = dict(page_no=1, slide_type=slide_type, title="测试")
    if slide_type is SlideType.TOC:
        kw["bullets"] = ["章节一"]
    elif slide_type is SlideType.TEXT_POINTS:
        kw["bullets"] = ["要点"]
    elif slide_type is SlideType.TABLE:
        kw["table"] = {"table_id": "t", "header": ["列"], "rows": [["值"]]}
    elif slide_type is SlideType.KEY_METRICS:
        kw["metrics"] = [{"label": "指标", "value": "1"}]
    elif slide_type is SlideType.TWO_COLUMN:
        kw["columns"] = [
            {"heading": "甲", "bullets": ["一"]},
            {"heading": "乙", "bullets": ["二"]},
        ]
    else:
        kw["subtitle"] = "副标题"
    return SlideIR(**kw)


def test_every_slide_type_has_layout():
    for t in SlideType:
        layout = design.layout_for(_minimal(t))
        assert "ph_title" in layout.texts, f"{t} 缺 ph_title"
        assert layout.texts["ph_title"].w > 0


def test_texts_layout_sync_on_fixture(deck_all_types):
    """texts_for 的键必须都在 layout 里有对应规格——渲染与容量永不失同步的核心不变式。"""
    for ir in deck_all_types.slides:
        layout = design.layout_for(ir)
        tmap = design.texts_for(ir)
        missing = set(tmap) - set(layout.texts)
        assert not missing, f"p{ir.page_no} texts_for 有 layout 未定义的形状：{missing}"


def test_two_column_geometry():
    ir = _minimal(SlideType.TWO_COLUMN)
    layout = design.layout_for(ir)
    assert set(layout.texts) == {"ph_title", "ph_c1_head", "ph_c1_body", "ph_c2_head", "ph_c2_body"}
    assert any(r.name == "fx_divider" for r in layout.rects)
    c1, c2 = layout.texts["ph_c1_body"], layout.texts["ph_c2_body"]
    assert c1.x + c1.w < c2.x  # 两栏不重叠


def test_key_metrics_card_count_matches():
    ir = _minimal(SlideType.KEY_METRICS)
    ir.metrics = [{"label": "甲", "value": "1"}, {"label": "乙", "value": "2"}, {"label": "丙", "value": "3"}]
    layout = design.layout_for(ir)
    cards = [r for r in layout.rects if r.name.startswith("fx_card")]
    assert len(cards) == 3
    assert not any(r.x + r.w > design.SLIDE_W for r in cards)


def test_whitelist_covers_all_types():
    assert set(BLOCK_WHITELIST) == set(SlideType)


def test_table_max_size_fits_region():
    from app.schema.slideir import LIMITS

    data_rows = LIMITS["table_rows"] - 1  # 表头占一行
    max_h = design.TABLE_ROW_H_HEADER + data_rows * design.TABLE_ROW_H_DATA
    assert max_h <= design.TABLE_REGION[3]


def test_content_pages_have_footer():
    for t in (SlideType.TOC, SlideType.TEXT_POINTS, SlideType.TWO_COLUMN, SlideType.TABLE, SlideType.KEY_METRICS):
        assert design.layout_for(_minimal(t)).footer, t
    for t in (SlideType.COVER, SlideType.SECTION_HEADER, SlideType.CLOSING):
        assert not design.layout_for(_minimal(t)).footer, t
