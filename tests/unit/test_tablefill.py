from __future__ import annotations

import json

from app.llm.prompts import PromptManager
from app.pipeline.tablefill import (
    MIN_CELL_WEIGHT,
    _norm_key,
    candidate_cells,
    cell_passes,
    fill_all_tables,
    mask_numbers,
    unmask_numbers,
)
from app.schema.doctree import DocMeta, DocSection, DocTable, DocTree


def _tree(full_text: str, tables: list[DocTable]) -> DocTree:
    root = DocSection(section_id="sec-0000", level=0, title="表格文档")
    return DocTree(
        meta=DocMeta(source_format="docx", source_name="f.docx",
                     n_chars=len(full_text)),
        sections=[root], tables=tables, full_text=full_text)


class _NoLLM:
    def structured(self, *a, **kw):
        raise AssertionError("不应调用 LLM")


class _FakeClient:
    """按次序弹出预置回复；schema 忽略（测试自行保证类型正确）。"""

    def __init__(self, replies):
        self.calls: list[tuple] = []
        self._replies = list(replies)

    def structured(self, schema, messages, stage=None, **kw):
        self.calls.append((schema, messages, stage))
        if not self._replies:
            raise AssertionError("FakeClient 收到了多余的调用")
        return self._replies.pop(0)


# ---- 候选格分类 ----

def test_candidate_cells_classification():
    t = DocTable(
        table_id="tbl-001", section_id="sec-0001", n_rows=4, n_cols=2,
        header=["字段", "说明"],
        rows=[
            ["姓名", "张三"],
            ["年龄", "35"],
            ["工作职责", "统筹产线日常管理与设备运维督导，覆盖 12 条产线"],
            ["备注", "统筹产线管理与设备运维工作，见 ⟦1⟧ 号附件说明"],
        ])
    # 表头不可寻址；短格/纯数字格/含 ⟦ 的格照搬，仅长描述格入选
    assert list(candidate_cells(t)) == ["2,1"]


def test_min_cell_weight_boundary():
    assert MIN_CELL_WEIGHT == 12
    t = DocTable(table_id="tbl-001", section_id="sec-0001", n_rows=1, n_cols=1,
                 header=["说明"], rows=[["统筹产线日常管理督导工作"]])  # 12 当量恰好入选
    assert list(candidate_cells(t)) == ["0,0"]


# ---- mask / unmask ----

def test_mask_unmask_roundtrip():
    text = "预算 120 万元，其中市场推广 95 万元，占比 79.2%。"
    masked, tokens = mask_numbers(text)
    assert tokens == ["120", "95", "79.2"]
    assert "120" not in masked and "95" not in masked and "79.2" not in masked
    assert "⟦1⟧" in masked and "⟦2⟧" in masked and "⟦3⟧" in masked
    assert unmask_numbers(masked, tokens) == text


def test_mask_no_numbers():
    masked, tokens = mask_numbers("无任何数字的长文本描述内容")
    assert masked == "无任何数字的长文本描述内容" and tokens == []
    assert unmask_numbers(masked + "改写", tokens) == "无任何数字的长文本描述内容改写"


def test_unmask_rejects_violations():
    masked, tokens = mask_numbers("覆盖 12 条产线与 300 台设备")
    assert unmask_numbers(masked.replace("⟦1⟧", ""), tokens) is None  # 删除占位符
    assert unmask_numbers(masked.replace("⟦1⟧", "⟦2⟧"), tokens) is None  # 错位
    assert unmask_numbers(masked.replace("⟦1⟧", "⟦9⟧"), tokens) is None  # 越界
    assert unmask_numbers(masked.replace("⟦1⟧", "⟦1⟧⟦1⟧"), tokens) is None  # 重复
    assert unmask_numbers(masked + "另含 45 项", tokens) is None  # 新增明文数字
    assert unmask_numbers(masked + "见⟦x⟧附件", tokens) is None  # 非法占位符


# ---- 复检 ----

def test_cell_passes():
    full = "该同志负责产线管理与设备运维的统筹工作，覆盖 12 条产线。"
    assert cell_passes("产线管理与设备运维由该同志统筹负责，覆盖 12 条产线。", full)
    assert not cell_passes(full, full)  # 照抄 → ngram 命中
    assert not cell_passes("覆盖 1200 条产线的统筹工作由该同志负责", full)  # 编造数字


# ---- fill_all_tables：零候选零 LLM ----

def test_fill_all_tables_zero_llm_without_candidates(tmp_path):
    t = DocTable(table_id="tbl-001", section_id="sec-0001", n_rows=2, n_cols=2,
                 header=["指标", "实际"],
                 rows=[["园区数", "12"], ["完成率", "98.6%"]])
    tree = _tree("指标 实际 园区数 12 完成率 98.6", [t])
    rewrites, report = fill_all_tables(tree, _NoLLM(), PromptManager(),
                                       tmp_path / "tables")
    assert rewrites == {"tbl-001": {}}
    assert report == []
    art = json.loads((tmp_path / "tables" / "tbl-001.json").read_text(
        encoding="utf-8"))
    assert art == {"table_id": "tbl-001", "cells": {}, "fallbacks": {}}
    # 断点续跑：二次调用同样零 LLM
    rewrites2, report2 = fill_all_tables(tree, _NoLLM(), PromptManager(),
                                         tmp_path / "tables")
    assert rewrites2 == rewrites and report2 == report


# ---- fill_all_tables：改写 + 兜底 ----

_LONG_A = "统筹产线日常管理与设备运维督导，覆盖 12 条产线"
_LONG_B = "负责区域市场渠道拓展与重点客户维护，新增 45 家渠道"


def _two_candidate_tree() -> DocTree:
    t = DocTable(
        table_id="tbl-001", section_id="sec-0001", n_rows=2, n_cols=2,
        header=["项目", "工作说明"],
        rows=[["岗位职责一", _LONG_A], ["岗位职责二", _LONG_B]])
    return _tree(f"岗位职责一 {_LONG_A} 岗位职责二 {_LONG_B}", [t])


def test_fill_all_tables_rewrite_and_fallback(tmp_path):
    from app.pipeline.tablefill import _CellsOut

    tree = _two_candidate_tree()
    masked_a, tokens_a = mask_numbers(_LONG_A)
    good_b = "区域市场渠道拓展及重点客户维护由其负责，新增 ⟦1⟧ 家渠道伙伴"
    client = _FakeClient([_CellsOut(cells={
        "0,1": masked_a,  # 原样照抄（占位符齐全）→ 复检 ngram 命中 → 退格
        "1,1": good_b,
    })])
    rewrites, report = fill_all_tables(tree, client, PromptManager(),
                                       tmp_path / "tables")
    assert client.calls[0][2] == "tablefill/tbl-001"
    assert len(client.calls) == 1  # 单表单调用
    # 明文数字永不进入 prompt
    user_msg = client.calls[0][1][1]["content"]
    assert "12" not in user_msg and "45" not in user_msg
    assert "⟦1⟧" in user_msg
    # 短格/表头不进 prompt
    assert "岗位职责一" not in user_msg and "项目" not in user_msg

    want_b = "区域市场渠道拓展及重点客户维护由其负责，新增 45 家渠道伙伴"
    assert rewrites == {"tbl-001": {"1,1": want_b}}
    assert report == [
        "[tablefill] W-CELL-FALLBACK tbl-001 0,1: 改写后未过复检（雷同/数字）"]
    art = json.loads((tmp_path / "tables" / "tbl-001.json").read_text(
        encoding="utf-8"))
    assert art["cells"] == {"1,1": want_b}
    assert art["fallbacks"] == {"0,1": "改写后未过复检（雷同/数字）"}

    # 断点续跑：有效产物 → 零 LLM，结果与报告可复现
    rewrites2, report2 = fill_all_tables(tree, _NoLLM(), PromptManager(),
                                         tmp_path / "tables")
    assert rewrites2 == rewrites and report2 == report


def test_fill_all_tables_placeholder_violation_falls_back(tmp_path):
    from app.pipeline.tablefill import _CellsOut

    tree = _two_candidate_tree()
    client = _FakeClient([_CellsOut(cells={
        "0,1": "统筹产线日常管理与设备运维督导",  # 丢了 ⟦1⟧ → 回填失败
        "1,1": "负责渠道拓展与客户维护，新增 ⟦1⟧ 家",  # 完整 → 通过
    })])
    rewrites, report = fill_all_tables(tree, client, PromptManager(),
                                       tmp_path / "tables")
    assert list(rewrites["tbl-001"]) == ["1,1"]
    assert report == [
        "[tablefill] W-CELL-FALLBACK tbl-001 0,1: 占位符回填失败"]


def test_fill_all_tables_client_failure_all_fallback(tmp_path):
    class _Boom:
        def structured(self, *a, **kw):
            raise RuntimeError("接口超时")

    tree = _two_candidate_tree()
    rewrites, report = fill_all_tables(tree, _Boom(), PromptManager(),
                                       tmp_path / "tables")
    assert rewrites == {"tbl-001": {}}
    assert report == [
        "[tablefill] W-CELL-FALLBACK tbl-001 0,1: LLM 调用失败",
        "[tablefill] W-CELL-FALLBACK tbl-001 1,1: LLM 调用失败",
    ]


def test_fill_all_tables_redoes_invalid_artifact(tmp_path):
    from app.pipeline.tablefill import _CellsOut

    tree = _two_candidate_tree()
    tables = tmp_path / "tables"
    tables.mkdir()
    # 伪劣断点产物：格改写文本与源文整段雷同（复检不过）→ 整表重做
    (tables / "tbl-001.json").write_text(json.dumps(
        {"table_id": "tbl-001", "cells": {"0,1": _LONG_A}, "fallbacks": {}},
        ensure_ascii=False), encoding="utf-8")
    fixed = "日常产线管理与运维督导由其统筹，覆盖 ⟦1⟧ 条产线"
    fixed_b = "重点客户维护及区域渠道拓展由其负责，新增 ⟦1⟧ 家渠道"
    client = _FakeClient([_CellsOut(cells={"0,1": fixed, "1,1": fixed_b})])
    rewrites, report = fill_all_tables(tree, client, PromptManager(), tables)
    assert len(client.calls) == 1  # 整表重做一次
    assert rewrites["tbl-001"] == {
        "0,1": fixed.replace("⟦1⟧", "12"),
        "1,1": fixed_b.replace("⟦1⟧", "45"),
    }
    assert report == []


def test_norm_key_accepts_llm_variants():
    assert _norm_key("2,3") == "2,3"
    assert _norm_key("[2,3]") == "2,3"
    assert _norm_key("2，3") == "2,3"
    assert _norm_key("（2,3）") == "2,3"
