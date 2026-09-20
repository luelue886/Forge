from __future__ import annotations

import json

from app.llm.prompts import PromptManager
from app.pipeline.tblvisual import (
    _col_weights,
    _fallback_visual,
    _table_stats,
    run_visual,
)
from app.schema.doctree import DocMeta, DocSection, DocTable, DocTree
from app.schema.tblskeleton import TCell, TRow, TSkeleton, TVisualTable


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
    assert visuals[0].col_widths == [20, 50, 30]
    assert visuals[0].row_heights == [28, 24, 80]
    assert report == []
    art = json.loads((tmp_path / "tblvisual.json").read_text(encoding="utf-8"))
    assert art["source"] == "llm" and art["schema"] == "tblvisual/1.0"


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
    assert visuals[0].col_widths == [20, 50, 30] and report == []


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
    assert visuals[0].col_widths == [20, 50, 30]
