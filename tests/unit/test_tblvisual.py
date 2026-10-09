from __future__ import annotations

import json

from app.llm.prompts import PromptManager
from app.pipeline.tblvisual import (
    _base_widths,
    _col_weights,
    _fallback_visual,
    _table_stats,
    enforce_photo_heights,
    enforce_photo_widths,
    run_visual,
)
from app.schema.doctree import DocMeta, DocSection, DocTable, DocTree
from app.schema.tblskeleton import (
    TCell,
    TRow,
    TSkeleton,
    TVisualTable,
    is_photo_cell,
    photo_size_cm,
)


class _FakeClient:
    def __init__(self, replies):
        self.calls: list[tuple] = []
        self._replies = list(replies)

    def structured(self, schema, messages, stage=None, **kw):
        self.calls.append((schema, messages, stage))
        if not self._replies:
            raise AssertionError("FakeClient 收到了多余的调用")
        return self._replies.pop(0)


class _NoLLM:
    def structured(self, *a, **kw):
        raise AssertionError("不应调用 LLM")


def _skeleton() -> TSkeleton:
    return TSkeleton(
        table_title="人员信息表", total_cols=3, src_tables=["tbl-001"],
        rows=[
            TRow(cells=[TCell(content="字段", style="header"),
                        TCell(content="内容", style="header"),
                        TCell(content="备注", style="header")]),
            TRow(cells=[TCell(content="姓名", style="label"),
                        TCell(content="统筹产线日常管理与设备运维督导，覆盖全部条线"),
                        TCell(content="", style="input")]),
            TRow(cells=[TCell(content="性别", style="label"),
                        TCell(content="男"),
                        TCell(content="")]),
        ])


def _tree(tables: list[DocTable]) -> DocTree:
    root = DocSection(section_id="sec-0000", level=0, title="表")
    return DocTree(
        meta=DocMeta(source_format="pdf", source_name="f.pdf", n_chars=10),
        sections=[root], tables=tables, full_text="表")


# ---- 确定性辅助 ----

def test_col_weights_spread_by_colspan():
    s = TSkeleton(table_title="t", total_cols=2, rows=[
        TRow(cells=[TCell(content="姓名", style="label"),
                    TCell(content="统筹产线管理", colspan=2, style="input")]),
        TRow(cells=[TCell(content="性别", style="label")]),
    ])
    w = _col_weights(s)
    # colspan=2 的格当量摊到两列；text_weight 汉字每字 1.0
    assert w[0] == 4.0  # 姓名 2 + 性别 2
    assert w[1] == 3.0  # 统筹产线管理 6 / 2


def test_table_stats_mentions_writing_rows():
    s = _skeleton()
    stats = _table_stats(0, s)
    assert "书写行 [1, 2]" in stats and "3列×3行" in stats


def test_fallback_visual_prefers_source_widths():
    t = DocTable(table_id="tbl-001", section_id="s", n_rows=2, n_cols=3,
                 header=["a", "b", "c"], rows=[["1", "2", "3"]],
                 col_widths=[0.2, 0.5, 0.3])
    tree = _tree([t])
    v = _fallback_visual(0, _skeleton(), tree)
    assert v.col_widths == [20, 50, 30] and v.row_heights is None


def test_fallback_visual_content_weighted_and_clamped():
    # 无源列宽 → 内容当量加权；窄列抬到 4%
    s = TSkeleton(table_title="t", total_cols=3, rows=[
        TRow(cells=[TCell(content="姓", style="label"),
                    TCell(content="统筹产线日常管理与设备运维督导覆盖条线"),
                    TCell(content="备", style="label")]),
    ])
    v = _fallback_visual(0, s, _tree([]))
    assert sum(v.col_widths) == 100 and all(w >= 4 for w in v.col_widths)
    assert v.col_widths[1] > v.col_widths[0]  # 当量大 → 列宽大


# ---- 照片格识别与尺寸（F1/F2 公共工具）----

def test_photo_size_cm_patterns():
    assert photo_size_cm("正面免冠彩色照片(2寸)") == (3.5, 4.9)
    assert photo_size_cm("相片（二寸）") == (3.5, 4.9)
    assert photo_size_cm("一寸照片") == (2.5, 3.5)
    assert photo_size_cm("照片") == (3.5, 4.9)  # 缺省 2寸
    assert photo_size_cm("小二寸照片") == (3.3, 4.8)
    assert photo_size_cm("") is None
    assert photo_size_cm("姓名") is None
    long_note = ("照片须采用近期拍摄的正面免冠半身彩色照，尺寸为二寸，"
                 "背面注明姓名及出生年月")  # 长说明文本不算照片格
    assert photo_size_cm(long_note) is None
    # 掩码态：架构师循环内标注数字已替换为 ⟦N⟧，判定不受影响（缺省 2寸）
    assert photo_size_cm("正面免冠彩色照片(⟦17⟧寸)") == (3.5, 4.9)


def test_is_photo_cell():
    assert is_photo_cell(TCell(content="照片", style="input"))
    assert not is_photo_cell(TCell(content="曾用名", style="label"))


# ---- 确定性列宽基准（F1）----

def test_base_widths_prefers_source_proportions():
    # 干部履历表实录：源比例 24/76，LLM 曾给成 96/4（完全颠倒）
    s = TSkeleton(table_title="t", total_cols=2, src_tables=["tbl-001"],
                  rows=[TRow(cells=[
                      TCell(content="何年何月何机关授予何种军警衔",
                            style="label"),
                      TCell(content="", style="input")])])
    t = DocTable(table_id="tbl-001", section_id="s", n_rows=1, n_cols=2,
                 header=["a", "b"], rows=[["1", "2"]],
                 col_widths=[0.243, 0.757])
    assert _base_widths(s, _tree([t])) == [24, 76]


def test_base_widths_content_placeholder_for_inputs():
    # 无先验：长标签封顶 + 空输入列占位当量，防书写区被挤没
    s = TSkeleton(table_title="t", total_cols=2, rows=[TRow(cells=[
        TCell(content="何年何月何机关授予何种军警衔", style="label"),
        TCell(content="", style="input")])])
    w = _base_widths(s, _tree([]))
    assert sum(w) == 100 and all(x >= 4 for x in w)
    assert w[1] >= 30  # 书写区至少三成


def test_base_widths_photo_min_width():
    # 源先验照片列偏窄 → 钳到 ≥ 2寸宽（3.5cm ≈ 24% 版心）
    s = TSkeleton(table_title="t", total_cols=2, src_tables=["tbl-001"],
                  rows=[TRow(cells=[
                      TCell(content="姓名", style="label"),
                      TCell(content="正面免冠彩色照片(2寸)",
                            style="input")])])
    t = DocTable(table_id="tbl-001", section_id="s", n_rows=1, n_cols=2,
                 header=["a", "b"], rows=[["1", "2"]],
                 col_widths=[0.85, 0.15])
    w = _base_widths(s, _tree([t]))
    assert w[1] >= round(3.5 / 14.64 * 100) and sum(w) == 100


def test_enforce_photo_widths_feedback_clamp():
    # 抽检意见重跑后的列宽同样受照片最小宽钳制（幂等最小值）
    s = TSkeleton(table_title="t", total_cols=2, rows=[TRow(cells=[
        TCell(content="姓名", style="label"),
        TCell(content="照片", style="input")])])
    w = enforce_photo_widths([90, 10], s)
    assert w[1] >= round(3.5 / 14.64 * 100) and sum(w) == 100


# ---- 源列宽先验审计（F1 修正：语义反转修复 / 垄断弃用）----

def _qa_band_skeleton(label: str = "何年何月何机关授予何种军警衔") -> TSkeleton:
    return TSkeleton(table_title="t", total_cols=2, src_tables=["tbl-001"],
                     rows=[TRow(cells=[
                         TCell(content=label, style="label"),
                         TCell(content="", style="input")])])


def _prior_tree(widths: list[float]) -> DocTree:
    t = DocTable(table_id="tbl-001", section_id="s", n_rows=1,
                 n_cols=len(widths), header=["a"] * len(widths),
                 rows=[["1"] * len(widths)], col_widths=widths)
    return _tree([t])


def test_base_widths_monopoly_prior_rejected():
    # 原生 docx 实录：问答带 grid [96,4] 是死数据——书写列 4% 放不下一个字。
    # 弃用先验走内容当量：长标签封顶 6、书写列按标签×0.8 占位 → 书写区拿回主宽
    w = _base_widths(_qa_band_skeleton(), _prior_tree([0.96, 0.04]))
    assert sum(w) == 100 and w[1] >= 60


def _id_form_skeleton() -> TSkeleton:
    """两行 姓名|书写|性别|书写 形态（修复对审计需逐行证据）。"""
    mk = lambda: TRow(cells=[
        TCell(content="姓名", style="label"),
        TCell(content="", style="input"),
        TCell(content="性别", style="label"),
        TCell(content="", style="input")])
    return TSkeleton(table_title="t", total_cols=4, src_tables=["tbl-001"],
                     rows=[mk(), mk()])


def test_base_widths_short_label_inversion_repaired():
    # 原生 docx 实录：t0 短标签列（姓名 2 字）先验 29% 配书写列 6%——
    # 只搬反转对（3:1 封顶，29-18=11 个点还给书写列），其余列照抄
    w = _base_widths(_id_form_skeleton(), _prior_tree([0.29, 0.06, 0.11, 0.54]))
    assert w == [18, 17, 11, 54]


def test_base_widths_sane_prior_kept():
    # 转换后 docx 实录（Word 重写的真实版面 grid）：无语义反转对 → 照抄
    w = _base_widths(_id_form_skeleton(), _prior_tree([0.17, 0.158, 0.108, 0.564]))
    assert w == [17, 16, 11, 56]


def test_base_widths_long_label_wide_prior_kept():
    # 长标签列（>6 当量）宽于书写列是真实版面需求（标签确实长）→ 不修
    w = _base_widths(_qa_band_skeleton(), _prior_tree([0.243, 0.757]))
    assert w == [24, 76]


def test_run_visual_reports_prior_repairs(tmp_path):
    # 首轮报告行：垄断弃用 / 反转修复须写明（W 级可观测）
    bad = TVisualTable(table_index=0, col_widths=[96, 4], row_heights=[60])
    client = _FakeClient([type("Out", (), {"tables": [bad]})()])
    visuals, report = run_visual([_qa_band_skeleton("姓名")],
                                 _prior_tree([0.96, 0.04]), client,
                                 PromptManager(), tmp_path)
    assert any("源表列宽异常" in line and "单列垄断" in line
               for line in report)
    assert visuals[0].col_widths[1] >= 60

    client2 = _FakeClient([type("Out", (), {"tables": [
        TVisualTable(table_index=0, col_widths=[29, 6, 11, 54],
                     row_heights=[60, 60])]})()])
    visuals2, report2 = run_visual([_id_form_skeleton()],
                                   _prior_tree([0.29, 0.06, 0.11, 0.54]),
                                   client2, PromptManager(), tmp_path / "j2")
    assert any("短标签反转修正 1 对" in line for line in report2)
    assert visuals2[0].col_widths == [18, 17, 11, 54]


def _photo_span_skeleton() -> TSkeleton:
    """照片格跨 4 行（rowspan=4，占住 col1-2），下方行只余 col0。"""
    return TSkeleton(table_title="t", total_cols=3, rows=[
        TRow(cells=[TCell(content="姓名", style="label"),
                    TCell(content="照片", colspan=2, rowspan=4, style="input")]),
    ] + [TRow(cells=[TCell(content="备注", style="label")])
         for _ in range(3)])


def test_enforce_photo_heights_clamps_span_sum():
    s = _photo_span_skeleton()
    h = enforce_photo_heights([20, 20, 20, 20], s)
    assert h[:3] == [20, 20, 20]  # 差额只加到跨行末行
    assert sum(h) >= round(4.9 * 28.35)


def test_enforce_photo_heights_idempotent():
    s = _photo_span_skeleton()
    h1 = enforce_photo_heights([20, 20, 20, 20], s)
    assert enforce_photo_heights(h1, s) == h1  # 最小值语义，二次钳制不动


def test_run_visual_photo_height_clamped(tmp_path):
    """LLM 行高不足照片标准高 → 输出前确定性钳制（F2）。"""
    v = TVisualTable(table_index=0, col_widths=[30, 40, 30],
                     row_heights=[20, 20, 20, 20])
    client = _FakeClient([type("Out", (), {"tables": [v]})()])
    visuals, _ = run_visual([_photo_span_skeleton()], _tree([]), client,
                            PromptManager(), tmp_path)
    assert visuals[0].row_heights[:3] == [20, 20, 20]
    assert sum(visuals[0].row_heights) >= round(4.9 * 28.35)


# ---- run_visual：LLM 编排 ----

def _llm_visual() -> TVisualTable:
    return TVisualTable(table_index=0, col_widths=[20, 50, 30],
                        row_heights=[28, 24, 80])


def test_run_visual_llm_ok_and_renormalize(tmp_path):
    tree = _tree([])
    out = type("Out", (), {"tables": [_llm_visual()]})()
    client = _FakeClient([out])
    visuals, report = run_visual([_skeleton()], tree, client,
                                 PromptManager(), tmp_path)
    # F1：首轮 LLM 列宽弃用，输出确定性基准；行高保留 LLM
    assert visuals[0].col_widths == _base_widths(_skeleton(), tree)
    assert visuals[0].row_heights == [28, 24, 80]
    assert report == []
    art = json.loads((tmp_path / "tblvisual.json").read_text(encoding="utf-8"))
    assert art["source"] == "llm-heights+base-widths" \
        and art["schema"] == "tblvisual/1.0"


def test_run_visual_first_pass_overrides_llm_widths(tmp_path):
    """首轮 LLM 给烂列宽（96/4 反转实录）→ 输出仍为源比例基准。"""
    t = DocTable(table_id="tbl-001", section_id="s", n_rows=1, n_cols=2,
                 header=["a", "b"], rows=[["1", "2"]],
                 col_widths=[0.243, 0.757])
    tree = _tree([t])
    s = TSkeleton(table_title="t", total_cols=2, src_tables=["tbl-001"],
                  rows=[TRow(cells=[
                      TCell(content="何年何月何机关授予何种军警衔",
                            style="label"),
                      TCell(content="", style="input")])])
    bad = TVisualTable(table_index=0, col_widths=[96, 4], row_heights=[60])
    client = _FakeClient([type("Out", (), {"tables": [bad]})()])
    visuals, report = run_visual([s], tree, client, PromptManager(), tmp_path)
    assert visuals[0].col_widths == [24, 76]
    assert visuals[0].row_heights == [60]
    assert any("源表比例基准" in w for w in report)


def test_run_visual_llm_sum_off_renormalized(tmp_path):
    v = TVisualTable(table_index=0, col_widths=[20, 50, 31])
    client = _FakeClient([type("Out", (), {"tables": [v]})()])
    visuals, _ = run_visual([_skeleton()], _tree([]), client,
                            PromptManager(), tmp_path)
    assert sum(visuals[0].col_widths) == 100


def test_run_visual_retry_then_pass(tmp_path):
    bad = type("Out", (), {"tables": [TVisualTable(
        table_index=0, col_widths=[20, 50])]})()  # 列数不符
    good = type("Out", (), {"tables": [_llm_visual()]})()
    client = _FakeClient([bad, good])
    visuals, report = run_visual([_skeleton()], _tree([]), client,
                                 PromptManager(), tmp_path)
    assert len(client.calls) == 2
    assert "列数" in client.calls[1][1][1]["content"]
    assert visuals[0].col_widths == _base_widths(_skeleton(), _tree([]))
    assert visuals[0].row_heights == [28, 24, 80] and report == []


def test_run_visual_fallback_after_two_fails(tmp_path):
    bad = type("Out", (), {"tables": [TVisualTable(
        table_index=0, col_widths=[20, 50])]})()
    client = _FakeClient([bad, bad])
    visuals, report = run_visual([_skeleton()], _tree([]), client,
                                 PromptManager(), tmp_path)
    assert len(client.calls) == 2
    # 兜底：源碎片无 col_widths → 内容当量加权 + 行高不指定
    assert sum(visuals[0].col_widths) == 100 and visuals[0].row_heights is None
    assert any("W-VISUAL-FALLBACK" in w for w in report)
    art = json.loads((tmp_path / "tblvisual.json").read_text(encoding="utf-8"))
    assert art["source"] == "fallback"


def test_run_visual_llm_error_direct_fallback(tmp_path):
    class _Boom:
        def structured(self, *a, **kw):
            raise RuntimeError("provider down")

    visuals, report = run_visual([_skeleton()], _tree([]), _Boom(),
                                 PromptManager(), tmp_path)
    assert sum(visuals[0].col_widths) == 100
    assert any("W-VISUAL-FALLBACK" in w for w in report)


# ---- 断点续跑 ----

def test_run_visual_artifact_resume(tmp_path):
    tree = _tree([])
    client = _FakeClient([type("Out", (), {"tables": [_llm_visual()]})()])
    first = run_visual([_skeleton()], tree, client, PromptManager(), tmp_path)
    again = run_visual([_skeleton()], tree, _NoLLM(), PromptManager(), tmp_path)
    assert first == again


def test_run_visual_artifact_invalid_redone(tmp_path):
    (tmp_path / "tblvisual.json").write_text(json.dumps({
        "schema": "tblvisual/1.0", "source": "llm",
        "tables": [{"table_index": 0, "col_widths": [20, 50],
                    "row_heights": None}],  # 列数不符 → 重做
        "report": []}), encoding="utf-8")
    client = _FakeClient([type("Out", (), {"tables": [_llm_visual()]})()])
    visuals, _ = run_visual([_skeleton()], _tree([]), client,
                            PromptManager(), tmp_path)
    assert len(client.calls) == 1
    assert visuals[0].col_widths == _base_widths(_skeleton(), _tree([]))
    assert visuals[0].row_heights == [28, 24, 80]


# ---- feedback 模式（抽检意见重跑）----

def _fb_visual() -> TVisualTable:
    return TVisualTable(table_index=0, col_widths=[16, 50, 34],
                        row_heights=[28, 24, 90])


def test_run_visual_feedback_anchored_and_overwrites(tmp_path):
    """feedback 绕过幂等门：有产物仍调 LLM；prompt 含意见 + 旧参数锚。"""
    tree = _tree([])
    # 先跑一遍产出旧参数（首轮=确定性基准列宽 + LLM 行高）
    first, _ = run_visual(
        [_skeleton()], tree,
        _FakeClient([type("Out", (), {"tables": [_llm_visual()]})()]),
        PromptManager(), tmp_path)
    old_w = first[0].col_widths
    client = _FakeClient([type("Out", (), {"tables": [_fb_visual()]})()])
    visuals, report = run_visual(
        [_skeleton()], tree, client, PromptManager(), tmp_path,
        feedback=["第3列过窄文字竖排（建议：加宽第3列）"])
    assert len(client.calls) == 1  # 幂等门被绕过
    user = client.calls[0][1][1]["content"]
    assert "第3列过窄" in user and f'"col_widths": {old_w}' in user
    assert visuals[0].col_widths == [16, 50, 34]  # 意见重跑 LLM 列宽生效
    assert report == []
    art = json.loads((tmp_path / "tblvisual.json").read_text(encoding="utf-8"))
    assert art["source"] == "llm-feedback"


def test_run_visual_feedback_fail_keeps_old(tmp_path):
    """意见重跑失败 → 保留旧参数 + W 行（不走确定性兜底防震荡）。"""
    tree = _tree([])
    first, _ = run_visual(
        [_skeleton()], tree,
        _FakeClient([type("Out", (), {"tables": [_llm_visual()]})()]),
        PromptManager(), tmp_path)
    old_w = first[0].col_widths

    class _Boom:
        def structured(self, *a, **kw):
            raise RuntimeError("down")

    visuals, report = run_visual(
        [_skeleton()], tree, _Boom(), PromptManager(), tmp_path,
        feedback=["意见"])
    assert visuals[0].col_widths == old_w  # 旧参数
    assert any("W-VISUAL-FEEDBACK-FAIL" in w for w in report)


def test_run_visual_feedback_no_old_params(tmp_path):
    """无旧参数可锚 → 不调 LLM，返回空 + W 行（调用方按无旋钮处理）。"""
    visuals, report = run_visual(
        [_skeleton()], _tree([]), _NoLLM(), PromptManager(), tmp_path,
        feedback=["意见"])
    assert visuals == []
    assert any("W-VISUAL-FEEDBACK-FAIL" in w for w in report)
