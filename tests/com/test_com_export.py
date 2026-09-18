from __future__ import annotations

from pathlib import Path

import pytest

from app.render.renderer import render_to_file
from app.schema.enums import IssueCode
from app.services.com_export import checklist, export_pngs

pytestmark = pytest.mark.com


@pytest.fixture(scope="module")
def rendered(tmp_path_factory, deck_all_types) -> Path:
    out = tmp_path_factory.mktemp("com") / "deck.pptx"
    render_to_file(deck_all_types, "business_blue", out)
    return out


def test_export_pngs_count(rendered, tmp_path):
    pngs = export_pngs(rendered, tmp_path / "pngs")
    assert len(pngs) == 8
    for p in pngs:
        assert p.exists() and p.stat().st_size > 1000


def test_checklist_clean(rendered, deck_all_types):
    issues = checklist(deck_all_types, rendered)
    assert not [i for i in issues if i.code is IssueCode.E_RENDER_MISMATCH], \
        [i.detail for i in issues]


def test_checklist_detects_page_mismatch(rendered, deck_all_types):
    from app.schema.slideir import Deck

    tampered = Deck.model_validate(deck_all_types.model_dump())
    tampered.slides = tampered.slides[:-1]  # 模拟 IR 与 pptx 页数不一致
    issues = checklist(tampered, rendered)
    assert any(i.code is IssueCode.E_RENDER_MISMATCH and "页数" in i.detail for i in issues)
