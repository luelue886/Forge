from __future__ import annotations

from pathlib import Path

import pytest

from app.render.tpl_catalog import (
    _capacity, extract_catalog, load_catalog, save_catalog,
    template_choices, template_pptx_path,
)
from app.schema.tplcatalog import TemplateCatalog
from tests.mk_tpl import build_synthetic_template


@pytest.fixture(scope="module")
def synthetic_tpl(tmp_path_factory) -> Path:
    return build_synthetic_template(tmp_path_factory.mktemp("tpl") / "synthetic.pptx")


@pytest.fixture(scope="module")
def catalog(synthetic_tpl: Path) -> TemplateCatalog:
    return extract_catalog(synthetic_tpl, "合成测试模板")


class TestExtract:
    def test_slide_count_and_order(self, catalog: TemplateCatalog):
        assert catalog.n_slides == 5
        assert [s.slide_no for s in catalog.slides] == [1, 2, 3, 4, 5]

    def test_cover_placeholders(self, catalog: TemplateCatalog):
        s1 = catalog.slide(1)
        roles = {slot.role for slot in s1.slots}
        assert "title" in roles
        title = s1.slots_of_role("title")[0]
        assert title.sample == "年度培训主题"
        assert title.font_pt == 40.0

    def test_toc_body_slot(self, catalog: TemplateCatalog):
        """多段落正文占位符 = 单 shape 单槽（版式无 xfrm，几何继承自母版）；段落以 " / " 拼入 sample。"""
        s2 = catalog.slide(2)
        bodies = s2.slots_of_role("body")
        assert len(bodies) == 1
        assert "第1章节" in bodies[0].sample
        assert len(bodies[0].sample) <= 40, "sample 截断到 40 字符"
        assert bodies[0].w > 5.0, "母版 body 几何（约 7 英寸宽）应被继承"

    def test_group_transform_math(self, catalog: TemplateCatalog):
        """组内槽绝对坐标 = off + (child − chOff) × ext/chExt。"""
        s3 = catalog.slide(3)
        grp_slots = [s for s in s3.slots if s.slot_id.startswith("2.")]
        assert len(grp_slots) == 4, "2×(标签+描述) 应全部在组内"
        label1 = next(s for s in grp_slots if s.sample == "团队协作")
        # child(100,200) → 绝对 (1.000, 2.000) 英寸；ext(140,40)×(6/300, 3/150) → (2.8, 0.8)
        assert label1.x == pytest.approx(1.0, abs=0.01)
        assert label1.y == pytest.approx(2.0, abs=0.01)
        assert label1.w == pytest.approx(140 * 6 / 300, abs=0.01)
        assert label1.h == pytest.approx(40 * 3 / 150, abs=0.01)
        label2 = next(s for s in grp_slots if s.sample == "数据驱动")
        assert label2.x == pytest.approx(1.0 + (260 - 100) * 6 / 300, abs=0.01)

    def test_non_ph_title_detected(self, catalog: TemplateCatalog):
        """无 ph 标题时：顶部 40% 区域 ≥24pt 最大字号者为 title。"""
        s3 = catalog.slide(3)
        titles = s3.slots_of_role("title")
        assert len(titles) == 1
        assert titles[0].sample == "内容页大标题"
        assert titles[0].font_pt == 28.0

    def test_page_no_and_design_roles(self, catalog: TemplateCatalog):
        s4 = catalog.slide(4)
        page_nos = s4.slots_of_role("page_no")
        assert len(page_nos) == 1
        assert page_nos[0].sample == "02 / 14"
        designs = s4.slots_of_role("design")
        assert any(d.sample == "·" for d in designs)

    def test_picture_not_a_slot(self, catalog: TemplateCatalog):
        s3 = catalog.slide(3)
        assert not any(s.sample == "" and s.role == "design" for s in s3.slots
                       if s.slot_id == "1"), "图片不应产生槽"

    def test_role_hints(self, catalog: TemplateCatalog):
        assert catalog.slide(1).role_hint == "cover"
        # S2 = 标题+大容量 body（Title and Content）：常规内容页而非表格宿主
        assert catalog.slide(2).role_hint == "content"
        assert catalog.slide(3).role_hint == "content"
        # S4 仅 1 个小文本框（容量 < 60）：表格宿主
        assert catalog.slide(4).role_hint == "table_host"
        assert catalog.slide(5).role_hint == "closing"

    def test_role_hint_toc_keyword(self):
        from app.render.tpl_catalog import _role_hint
        assert _role_hint("目录页", 3, 10, 5, 100.0) == "toc"
        assert _role_hint("Agenda", 3, 10, 5, 100.0) == "toc"
        assert _role_hint("节标题", 3, 10, 1, 10.0) == "section"
        # 英文成品整副挂 Title Slide 版式：中间页不得因此判 cover
        assert _role_hint("Title Slide", 3, 10, 5, 100.0) == "content"

    def test_signature_format(self, catalog: TemplateCatalog):
        sig = catalog.slide(3).signature
        assert sig.startswith("S3 布局=")
        assert "title≤" in sig

    def test_fillable_counts(self, catalog: TemplateCatalog):
        s3 = catalog.slide(3)
        # 标题 + 4 个组内槽
        assert s3.fillable >= 5


class TestCapacity:
    def test_formula(self):
        # 4×1 英寸框、16pt：lines=floor((1-0.06)/0.2778)=3, chars=floor((4-0.1)/0.2222)=17
        assert _capacity(4.0, 1.0, 16.0) == pytest.approx(3 * 17 * 0.85, abs=0.01)

    def test_clamped(self):
        assert _capacity(0.01, 0.01, 16.0) == 1.0
        assert _capacity(40.0, 40.0, 10.0) == 400.0


class TestPersistence:
    def test_save_load_roundtrip(self, synthetic_tpl: Path, tmp_path: Path):
        cat = extract_catalog(synthetic_tpl, "合成测试模板")
        stem = synthetic_tpl.stem
        (tmp_path / f"{stem}.pptx").write_bytes(synthetic_tpl.read_bytes())
        save_catalog(cat, tmp_path)
        loaded = load_catalog(stem, tmp_path)
        assert loaded.display_name == "合成测试模板"
        assert loaded.n_slides == 5

        choices = template_choices(tmp_path)
        assert choices == [(stem, "合成测试模板")]

    def test_load_missing(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="可用"):
            load_catalog("nope", tmp_path)

    def test_load_without_pptx(self, synthetic_tpl: Path, tmp_path: Path):
        cat = extract_catalog(synthetic_tpl)
        save_catalog(cat, tmp_path)
        with pytest.raises(FileNotFoundError, match="缺少 pptx"):
            load_catalog(synthetic_tpl.stem, tmp_path)
