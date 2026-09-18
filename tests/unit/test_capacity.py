from __future__ import annotations

import pytest

from app.render.capacity import (
    capacity_issues,
    check_slide,
    text_px,
    wrap_lines,
)
from app.schema.enums import IssueCode, Severity, SlideType
from tests.conftest import FONT_PATH

requires_font = pytest.mark.skipif(not FONT_PATH.exists(), reason=f"字体缺失：{FONT_PATH}")


@requires_font
def test_text_px_positive():
    assert text_px("测试文本", 16) > 0
    assert text_px("测试文本", 16) > text_px("测试", 16)


@requires_font
def test_wrap_lines_cjk():
    s = "一二三四五六"
    three = text_px("一二三", 16)
    assert wrap_lines(s, 16, three) == 2  # 恰好三字一行
    assert wrap_lines(s, 16, three * 3) == 1


@requires_font
def test_wrap_lines_ascii_word_atomic():
    # ASCII 词不拆行：宽度只够一半时整词换行
    w = text_px("abcdef", 14)
    assert wrap_lines("xx abcdef yy", 14, w * 0.6) >= 2


@requires_font
def test_all_types_fixture_no_blocking(deck_all_types):
    issues = capacity_issues(deck_all_types)
    blocking = [i for i in issues if i.severity is Severity.BLOCKING]
    assert not blocking, [i.detail for i in blocking]


@requires_font
def test_overflow_fixture_detected(deck_overflow):
    issues = capacity_issues(deck_overflow)
    overflows = [i for i in issues if i.code is IssueCode.E_OVERFLOW]
    assert overflows, "25 条目录应触发 E-OVERFLOW"
    assert all(i.page_no == 2 for i in overflows)


@requires_font
def test_overflow_ratio_magnitude(deck_overflow):
    toc = next(s for s in deck_overflow.slides if s.slide_type is SlideType.TOC)
    items = check_slide(toc)
    body = next(i for i in items if i.name == "ph_body")
    assert body.ratio > 1.5
