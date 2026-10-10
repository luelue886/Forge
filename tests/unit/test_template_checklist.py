"""M6 构建后 QA：template_checklist 的页数/遗留/空页核对（合成模板，零 COM）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.render.template_build import build_deck
from app.render.tpl_catalog import extract_catalog
from app.schema.enums import IssueCode, SlideType
from app.schema.slotfill import BuildMeta, DeckBuild, PageFill
from app.schema.tplcatalog import TemplateCatalog
from tests.mk_tpl import build_synthetic_template


@pytest.fixture(scope="module")
def catalog(tmp_path_factory) -> TemplateCatalog:
    tpl = build_synthetic_template(tmp_path_factory.mktemp("tpl") / "synthetic.pptx")
    return extract_catalog(tpl, "合成测试模板")


def _page(no: int, slide: int, values: dict[str, str],
          deletes: list[str] | None = None) -> PageFill:
    return PageFill(page_no=no, slide_type=SlideType.TEXT_POINTS,
                    template_slide=slide, title=f"页{no}",
                    slot_values=values, slot_deletes=deletes or [])


def _s3_ids(catalog: TemplateCatalog) -> dict:
    s3 = catalog.slide(3)
    return {"title": next(s.slot_id for s in s3.slots if s.role == "title"),
            "grp": sorted(s.slot_id for s in s3.slots if s.slot_id.startswith("2."))}


@pytest.fixture(scope="module")
def tpl_path(tmp_path_factory) -> Path:
    return build_synthetic_template(tmp_path_factory.mktemp("tpl2") / "synthetic.pptx")


def _build_deck(tpl_path, catalog, tmp_path, pages, name="out.pptx"):
    deck = DeckBuild(meta=BuildMeta(title="冒烟", template=catalog.stem), pages=pages)
    out = tmp_path / name
    build_deck(tpl_path, catalog, deck, out)
    return out


def test_clean(catalog, tpl_path, tmp_path):
    from app.services.com_export import template_checklist

    ids = _s3_ids(catalog)
    # N1 起 tagline 槽不再豁免：未填未删的模板原文会报 E_LEFTOVER，须显式删除
    s4_taglines = [s.slot_id for s in catalog.slide(4).slots_of_role("tagline")]
    pages = [
        _page(1, 3, {ids["title"]: "覆盖规模", **{g: "要点" for g in ids["grp"]}}),
        _page(2, 4, {"0": "独立正文"}, deletes=s4_taglines),  # page_no/design 位不触碰属正常
    ]
    pptx = _build_deck(tpl_path, catalog, tmp_path, pages)
    issues = template_checklist(
        DeckBuild(meta=BuildMeta(title="t", template=catalog.stem), pages=pages),
        catalog, pptx)
    assert issues == [], [i.detail for i in issues]


def test_tagline_leftover_detected(catalog, tpl_path, tmp_path):
    """N1 盲区消除：tagline 槽的模板原文未填未删 → E_LEFTOVER（BLOCKING）。"""
    from app.services.com_export import template_checklist

    ids = _s3_ids(catalog)
    pages = [
        _page(1, 3, {ids["title"]: "覆盖规模", **{g: "要点" for g in ids["grp"]}}),
        _page(2, 4, {"0": "独立正文"}),  # tagline 槽未触碰
    ]
    pptx = _build_deck(tpl_path, catalog, tmp_path, pages)
    issues = template_checklist(
        DeckBuild(meta=BuildMeta(title="t", template=catalog.stem), pages=pages),
        catalog, pptx)
    leftovers = [i for i in issues if i.code is IssueCode.E_LEFTOVER]
    assert len(leftovers) == len(catalog.slide(4).slots_of_role("tagline"))
    assert any("单季全球总额" in i.detail for i in leftovers)


def test_leftover_detected(catalog, tpl_path, tmp_path):
    """槽未填充未删除 → 模板原文遗留（组内文本也要能查到）。"""
    from app.services.com_export import template_checklist

    ids = _s3_ids(catalog)
    # 只填 title、只删 2.0；2.1~2.3 未覆盖 → 原文遗留
    pages = [_page(1, 3, {ids["title"]: "覆盖规模"}, deletes=[ids["grp"][0]])]
    pptx = _build_deck(tpl_path, catalog, tmp_path, pages)
    issues = template_checklist(
        DeckBuild(meta=BuildMeta(title="t", template=catalog.stem), pages=pages),
        catalog, pptx)
    leftovers = [i for i in issues if i.code is IssueCode.E_LEFTOVER]
    assert len(leftovers) == 3  # 2.1/2.2/2.3 各一条
    assert any("跨部门协同" in i.detail for i in leftovers)


def test_deleted_slot_not_flagged(catalog, tpl_path, tmp_path):
    """已删槽不查 + 填充值含其 sample 段不误报（swiss e2e 实弹：删掉的
    "Helvetica" 命中 design 槽保留文案被误报 E_LEFTOVER）。"""
    from app.services.com_export import template_checklist

    ids = _s3_ids(catalog)
    pages = [_page(1, 3, {ids["title"]: "团队协作推进"}, deletes=ids["grp"])]
    pptx = _build_deck(tpl_path, catalog, tmp_path, pages)
    issues = template_checklist(
        DeckBuild(meta=BuildMeta(title="t", template=catalog.stem), pages=pages),
        catalog, pptx)
    assert not any(i.code is IssueCode.E_LEFTOVER for i in issues), \
        [i.detail for i in issues]


def test_legit_substring_not_flagged(catalog, tpl_path, tmp_path):
    """未删槽的 sample 段若被已填文本合法包含，不算遗留；真遗留照报。"""
    from app.services.com_export import template_checklist

    ids = _s3_ids(catalog)
    # title 填充值含 2.0 的 sample「团队协作」→ 2.0 免报；2.1~2.3 真遗留
    pages = [_page(1, 3, {ids["title"]: "团队协作要点"})]
    pptx = _build_deck(tpl_path, catalog, tmp_path, pages)
    issues = template_checklist(
        DeckBuild(meta=BuildMeta(title="t", template=catalog.stem), pages=pages),
        catalog, pptx)
    leftovers = [i for i in issues if i.code is IssueCode.E_LEFTOVER]
    assert len(leftovers) == 3
    assert not any("团队协作" in i.detail for i in leftovers)


def test_empty_page_detected(catalog, tpl_path, tmp_path):
    """非 design 槽全部删除且无表格 → 页面无任何文本。"""
    from app.services.com_export import template_checklist

    ids = _s3_ids(catalog)
    pages = [_page(1, 3, {}, deletes=[ids["title"]] + ids["grp"])]
    pptx = _build_deck(tpl_path, catalog, tmp_path, pages)
    issues = template_checklist(
        DeckBuild(meta=BuildMeta(title="t", template=catalog.stem), pages=pages),
        catalog, pptx)
    assert any(i.code is IssueCode.E_EMPTY_PLACEHOLDER for i in issues)


def test_page_count_mismatch(catalog, tpl_path, tmp_path):
    from app.services.com_export import template_checklist

    ids = _s3_ids(catalog)
    pages = [_page(1, 3, {ids["title"]: "一页"}),
             _page(2, 4, {"0": "两页"})]
    pptx = _build_deck(tpl_path, catalog, tmp_path, pages)
    tampered = DeckBuild(meta=BuildMeta(title="t", template=catalog.stem),
                         pages=pages[:1])
    issues = template_checklist(tampered, catalog, pptx)
    assert any(i.code is IssueCode.E_RENDER_MISMATCH and "页数" in i.detail
               for i in issues)
