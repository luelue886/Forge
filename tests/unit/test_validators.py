from __future__ import annotations

from app.schema.enums import IssueCode, SlideType
from app.schema.slideir import (
    LIMITS,
    Column,
    Deck,
    Metric,
    SlideIR,
    TableIR,
    validate_deck,
    validate_slide,
)

CJK_20 = "一二三四五六七八九十一二三四五六七八九十"
CJK_21 = CJK_20 + "一"
CJK_40 = CJK_20 * 2
CJK_41 = CJK_40 + "一"


def slide(**kw) -> SlideIR:
    base = dict(page_no=1, slide_type=SlideType.TEXT_POINTS, title="测试页", bullets=["要点一", "要点二"])
    base.update(kw)
    return SlideIR(**base)


def deck(*slides: SlideIR) -> Deck:
    return Deck(meta={"title": "测试"}, slides=list(slides))


def rules(issues):
    return [i.rule for i in issues]


def test_title_boundary():
    assert IssueCode.V_TITLE_LEN not in rules(validate_slide(slide(title=CJK_20)))
    assert IssueCode.V_TITLE_LEN in rules(validate_slide(slide(title=CJK_21)))
    assert IssueCode.V_TITLE_LEN in rules(validate_slide(slide(title="   ")))


def test_bullet_count():
    assert IssueCode.V_BULLET_COUNT not in rules(validate_slide(slide(bullets=["一"] * 5)))
    assert IssueCode.V_BULLET_COUNT in rules(validate_slide(slide(bullets=["一"] * 6)))
    # toc 豁免条目数
    toc = slide(slide_type=SlideType.TOC, bullets=["章节一", "章节二", "章节三", "章节四", "章节五", "章节六"])
    assert IssueCode.V_BULLET_COUNT not in rules(validate_slide(toc))


def test_bullet_len():
    assert IssueCode.V_BULLET_LEN not in rules(validate_slide(slide(bullets=[CJK_40])))
    assert IssueCode.V_BULLET_LEN in rules(validate_slide(slide(bullets=[CJK_41])))


def test_metrics_range():
    assert IssueCode.V_METRICS not in rules(validate_slide(slide(metrics=[Metric(label="甲", value="1")] * 2, bullets=[])))
    assert IssueCode.V_METRICS not in rules(validate_slide(slide(metrics=[Metric(label="甲", value="1")] * 4, bullets=[])))
    assert IssueCode.V_METRICS in rules(validate_slide(slide(metrics=[Metric(label="甲", value="1")], bullets=[])))
    assert IssueCode.V_METRICS in rules(validate_slide(slide(metrics=[Metric(label="甲", value="1")] * 5, bullets=[])))


def _table(rows: int, cols: int) -> TableIR:
    return TableIR(
        table_id="t",
        header=[f"列{j}" for j in range(cols)],
        rows=[[f"{i}-{j}" for j in range(cols)] for i in range(rows)],
    )


def test_table_size():
    ok = slide(slide_type=SlideType.TABLE, bullets=[], table=_table(4, 6))  # 表头+4 数据行 = 5 行
    assert IssueCode.V_TABLE_SIZE not in rules(validate_slide(ok))
    too_many_rows = slide(slide_type=SlideType.TABLE, bullets=[], table=_table(5, 4))
    assert IssueCode.V_TABLE_SIZE in rules(validate_slide(too_many_rows))
    too_many_cols = slide(slide_type=SlideType.TABLE, bullets=[], table=_table(3, 7))
    assert IssueCode.V_TABLE_SIZE in rules(validate_slide(too_many_cols))


def test_whitelist():
    cover_with_bullets = slide(slide_type=SlideType.COVER, subtitle="副标题")
    assert IssueCode.V_WHITELIST in rules(validate_slide(cover_with_bullets))

    text_points_with_table = slide(table=_table(2, 2))
    assert IssueCode.V_WHITELIST in rules(validate_slide(text_points_with_table))

    one_column = slide(
        slide_type=SlideType.TWO_COLUMN, bullets=[],
        columns=[Column(heading="甲", bullets=["一"])],
    )
    assert IssueCode.V_WHITELIST in rules(validate_slide(one_column))

    no_metrics = slide(slide_type=SlideType.KEY_METRICS, bullets=[])
    assert IssueCode.V_WHITELIST in rules(validate_slide(no_metrics))

    table_no_table = slide(slide_type=SlideType.TABLE, bullets=[])
    assert IssueCode.V_WHITELIST in rules(validate_slide(table_no_table))


def test_page_budget():
    ok = slide(bullets=[CJK_40] * 5)  # 200 当量，恰好不超
    assert IssueCode.V_PAGE_BUDGET not in rules(validate_slide(ok))
    over = slide(
        bullets=[CJK_40] * 5,
        metrics=[Metric(label=CJK_20 + "指标甲", value="1"), Metric(label=CJK_20 + "指标乙", value="2")],
    )
    assert IssueCode.V_PAGE_BUDGET in rules(validate_slide(over))
    # 结构页豁免
    cover = slide(slide_type=SlideType.COVER, subtitle="", bullets=[CJK_41] * 6)
    assert IssueCode.V_PAGE_BUDGET not in rules(validate_slide(cover))


def test_page_no_continuity():
    gap = deck(slide(page_no=1), slide(page_no=3, title="第二页"))
    assert IssueCode.V_PAGE_NO in rules(validate_deck(gap))
    dup = deck(slide(page_no=1), slide(page_no=1, title="重复"))
    assert IssueCode.V_PAGE_NO in rules(validate_deck(dup))
    ok = deck(slide(page_no=1), slide(page_no=2, title="第二页"))
    assert IssueCode.V_PAGE_NO not in rules(validate_deck(ok))
    assert validate_deck(Deck(meta={"title": "空"}, slides=[])) == []


def test_all_types_fixture_valid(deck_all_types):
    issues = validate_deck(deck_all_types)
    assert issues == [], [f"p{i.page_no} {i.rule.value}: {i.detail}" for i in issues]


def test_overflow_fixture_schema_valid_but_heavy(deck_overflow):
    """溢出 fixture 必须通过 schema（ toc 豁免），只由容量检查负责拦截。"""
    assert validate_deck(deck_overflow) == []


def test_limits_constant_sanity():
    assert LIMITS["table_rows"] == 5 and LIMITS["table_cols"] == 6
    assert LIMITS["title_weight"] == 20.0 and LIMITS["page_budget"] == 200.0
