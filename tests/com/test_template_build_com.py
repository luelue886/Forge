"""真模板构建 COM 冒烟：中文占位符模板 + 英文 Group 构图模板各 3 页 → PNG 导出。

门禁验证：raw-XML 复制/替换/清理 + python-pptx 再存后，PowerPoint COM 仍可打开导出。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PPTX_TEMPLATES_DIR
from app.render.template_build import build_deck
from app.render.tpl_catalog import load_catalog, template_pptx_path
from app.schema.enums import SlideType
from app.schema.slotfill import BuildMeta, DeckBuild, PageFill
from app.services.com_export import export_pngs

pytestmark = pytest.mark.com


def _smoke_pages(cat, slide_nos: list[int]) -> list[PageFill]:
    pages: list[PageFill] = []
    for no in slide_nos:
        s = cat.slide(no)
        values = {
            slot.slot_id: ("冒烟标题验证" if slot.role == "title" else "冒烟槽位")
            for slot in s.slots if slot.fillable
        }
        pages.append(PageFill(
            page_no=len(pages) + 1, slide_type=SlideType.TEXT_POINTS,
            template_slide=no, title="冒烟", slot_values=values, note="冒烟备注"))
    return pages


def _build(stem: str, slide_nos: list[int], out: Path) -> Path:
    cat = load_catalog(stem, PPTX_TEMPLATES_DIR)
    return build_deck(
        template_pptx_path(stem, PPTX_TEMPLATES_DIR), cat,
        DeckBuild(meta=BuildMeta(title="冒烟", template=cat.stem),
                  pages=_smoke_pages(cat, slide_nos)),
        out)


@pytest.fixture(scope="module")
def zh_built(tmp_path_factory) -> Path:
    return _build("办公技巧商务培训PPT模板", [1, 8, 33],
                  tmp_path_factory.mktemp("smoke_zh") / "zh.pptx")


@pytest.fixture(scope="module")
def en_built(tmp_path_factory) -> Path:
    return _build("pritzker_2026", [1, 2, 11],
                  tmp_path_factory.mktemp("smoke_en") / "en.pptx")


def test_zh_placeholder_template_smoke(zh_built: Path, tmp_path: Path):
    pngs = export_pngs(zh_built, tmp_path / "zh_pngs")
    assert len(pngs) == 3
    assert all(p.stat().st_size > 1000 for p in pngs)


def test_en_group_template_smoke(en_built: Path, tmp_path: Path):
    pngs = export_pngs(en_built, tmp_path / "en_pngs")
    assert len(pngs) == 3
    assert all(p.stat().st_size > 1000 for p in pngs)
