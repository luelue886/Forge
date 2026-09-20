from __future__ import annotations

import json

import pytest

from app.llm.prompts import PromptManager
from app.pipeline.tblcontent import (
    _prose_candidates,
    _skeleton_candidates,
    run_content,
)
from app.schema.doctree import DocBlock, DocMeta, DocSection, DocTree
from app.schema.tblskeleton import TCell, TRow, TSkeleton


class _Out:
    """LLM 结构化输出替身：cells 即改写映射。"""

    def __init__(self, cells):
        self.cells = cells


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


LONG_TEXT = "统筹产线日常管理与设备运维督导，覆盖 12 条产线"
LONG_TEXT_MASKED = "统筹产线日常管理与设备运维督导，覆盖 ⟦1⟧ 条产线"
LONG_REWRITE = "负责产线管理及设备维护督导工作，涉及 ⟦1⟧ 条生产线"
LONG_REWRITE_FILLED = "负责产线管理及设备维护督导工作，涉及 12 条生产线"


def _skeleton() -> TSkeleton:
    return TSkeleton(
        table_title="人员信息表", total_cols=2, src_tables=["tbl-001"],
        rows=[
            TRow(cells=[TCell(content="字段", style="header"),
                        TCell(content="内容", style="header")]),
            TRow(cells=[TCell(content="姓名", style="label"),
                        TCell(content="张三", style="input")]),
            TRow(cells=[TCell(content="工作职责", style="label"),
                        TCell(content=LONG_TEXT, style="input")]),
        ])


def _tree(prose=("本表用于年度绩效考核与员工发展跟踪评估",)) -> DocTree:
    root = DocSection(section_id="sec-0000", level=0, title="表")
    root.blocks = [DocBlock(block_id=f"blk-{i:04d}", kind="para", text=p)
                   for i, p in enumerate(prose)]
    full = ("字段 内容 姓名 张三 工作职责 " + LONG_TEXT + " " + " ".join(prose))
    return DocTree(
        meta=DocMeta(source_format="pdf", source_name="f.pdf", n_chars=len(full)),
        sections=[root], tables=[], full_text=full)


# ---- 候选分类 ----

def test_skeleton_candidates_classification():
    report: list[str] = []
    cand = _skeleton_candidates(_skeleton(), 0, report)
    # 表头/label 照搬；值格配对字段名；长格走措辞改写
    assert cand == {
        "1,1": ("张三", "value", "姓名"),
        "2,1": (LONG_TEXT, "text", ""),
    }
    assert report == []


def test_skeleton_candidates_verbatim_rules():
    s = TSkeleton(table_title="t", total_cols=2, rows=[
        TRow(cells=[TCell(content="□是 □否", style="option"),
                    TCell(content="13800000000", style="input")]),
        TRow(cells=[TCell(content="备注", style="label"),
                    TCell(content="性别", style="input")]),  # 值本身是字段名
        TRow(cells=[TCell(content="状态", style="note"),
                    TCell(content="正常", style="note")]),  # 左邻非字段词汇
    ])
    report: list[str] = []
    cand = _skeleton_candidates(s, 0, report)
    assert cand == {}  # 选项/纯数字/字段名值/无字段左邻 → 全照搬


def test_skeleton_candidates_placeholder_leftover_reports_w():
    s = TSkeleton(table_title="t", total_cols=1, rows=[
        TRow(cells=[TCell(content="残留 ⟦9⟧ 占位符", style="input")])])
    report: list[str] = []
    cand = _skeleton_candidates(s, 0, report)
    assert cand == {}
    assert report == ["[tblcontent] W-PLACEHOLDER t0 r0c0: 骨架格残留占位符，照搬"]


def test_skeleton_candidates_rowspan_label_value():
    # 跨行 label（rowspan=2）右侧两行的值都能配对
    s = TSkeleton(table_title="t", total_cols=2, rows=[
        TRow(cells=[TCell(content="姓名", rowspan=2, style="label"),
                    TCell(content="张三", style="input")]),
        TRow(cells=[TCell(content="李四", style="input")]),
    ])
    cand = _skeleton_candidates(s, 0, [])
    assert cand == {
        "0,1": ("张三", "value", "姓名"),
        "1,1": ("李四", "value", "姓名"),
    }


def test_skeleton_candidates_header_above_identity_value():
    # 列式表头（姓名）正下方人形值 → 值；部门/日期列（非字段表头/含数字）照搬
    s = TSkeleton(table_title="t", total_cols=3, rows=[
        TRow(cells=[TCell(content="姓名", style="header"),
                    TCell(content="部门", style="header"),
                    TCell(content="入职日期", style="header")]),
        TRow(cells=[TCell(content="张伟", style="input"),
                    TCell(content="市场部", style="input"),
                    TCell(content="2021年3月", style="input")]),
    ])
    cand = _skeleton_candidates(s, 0, [])
    assert cand == {"1,0": ("张伟", "value", "姓名")}


def test_skeleton_candidates_header_above_shape_gates():
    # 字段表头下纯数字值不虚构；上方格非字段词汇（备注）不触发
    s = TSkeleton(table_title="t", total_cols=2, rows=[
        TRow(cells=[TCell(content="联系电话", style="header"),
                    TCell(content="备注", style="label")]),
        TRow(cells=[TCell(content="13800001234", style="input"),
                    TCell(content="王芳", style="input")]),  # 上方非字段词汇
    ])
    cand = _skeleton_candidates(s, 0, [])
    assert cand == {}


def test_skeleton_candidates_label_above_identity_value():
    # 人事表实测形状：架构师把身份栏顶行标成 label 而非 header——张伟列
    # （label+字段词汇）正下方的 input 人形值仍须虚构；非字段（部门）、
    # 含数字（日期/电话）照搬
    s = TSkeleton(table_title="t", total_cols=4, rows=[
        TRow(cells=[TCell(content="姓名", style="label"),
                    TCell(content="部门", style="label"),
                    TCell(content="入职日期", style="label"),
                    TCell(content="联系电话", style="label")]),
        TRow(cells=[TCell(content="张伟", style="input"),
                    TCell(content="市场部", style="input"),
                    TCell(content="2021年3月", style="input"),
                    TCell(content="13800001234", style="input")]),
    ])
    cand = _skeleton_candidates(s, 0, [])
    assert cand == {"1,0": ("张伟", "value", "姓名")}


def test_prose_candidates_weight_gate():
    tree = _tree(prose=("短句", "本表用于年度绩效考核与员工发展跟踪评估"))
    cand = _prose_candidates(tree)
    assert list(cand) == ["p0"]
    assert cand["p0"][1] == "text"


# ---- run_content：LLM 编排 ----

def test_run_content_mixed_and_masking(tmp_path):
    tree = _tree()
    client = _FakeClient([_Out({
        "t0 1,1": "李四",
        "t0 2,1": LONG_REWRITE,
        "p0": "此表服务于年度绩效评估与员工成长跟踪",
    })])
    tables, prose, report = run_content([_skeleton()], tree, client,
                                        PromptManager(), tmp_path)
    assert tables == {0: {"1,1": "李四", "2,1": LONG_REWRITE_FILLED}}
    assert prose == {"p0": "此表服务于年度绩效评估与员工成长跟踪"}
    assert report == []
    # 掩码铁律：prompt 无明文数字
    user_msg = client.calls[0][1][1]["content"]
    assert "12" not in user_msg and "⟦1⟧" in user_msg
    art = json.loads((tmp_path / "tblcontent.json").read_text(encoding="utf-8"))
    assert art["schema"] == "tblcontent/1.0" and len(art["cells"]) == 3


def test_run_content_key_normalization(tmp_path):
    tree = _tree()
    client = _FakeClient([_Out({
        "[t0 1，1]": "李四",  # LLM 写了括号+全角逗号
        "t0 2,1": LONG_REWRITE,
        "p0": "此表服务于年度绩效评估与员工成长跟踪",
    })])
    tables, _, _ = run_content([_skeleton()], tree, client, PromptManager(), tmp_path)
    assert tables[0]["1,1"] == "李四"


def test_run_content_retry_then_pass(tmp_path):
    tree = _tree()
    bad = _Out({"t0 1,1": "李四",
                "t0 2,1": LONG_TEXT_MASKED,  # 原文照抄（占位符保留）→ 10 字雷同
                "p0": "此表服务于年度绩效评估与员工成长跟踪"})
    good = _Out({"t0 2,1": LONG_REWRITE})
    client = _FakeClient([bad, good])
    tables, _, _ = run_content([_skeleton()], tree, client, PromptManager(), tmp_path)
    assert len(client.calls) == 2
    retry_user = client.calls[1][1][1]["content"]
    assert "未过复检" in retry_user and "t0 2,1" in retry_user
    assert tables[0]["2,1"] == LONG_REWRITE_FILLED
    assert tables[0]["1,1"] == "李四"


def test_run_content_retry_fail_falls_back(tmp_path):
    tree = _tree()
    bad = _Out({"t0 1,1": "李四", "t0 2,1": LONG_TEXT_MASKED,
                "p0": "此表服务于年度绩效评估与员工成长跟踪"})
    bad2 = _Out({"t0 2,1": LONG_TEXT_MASKED})
    client = _FakeClient([bad, bad2])
    tables, prose, report = run_content([_skeleton()], tree, client,
                                        PromptManager(), tmp_path)
    # 两轮都雷同 → 该项退回照搬（不在 cells 映射中）+ W 报告行
    assert "2,1" not in tables[0]
    assert tables[0]["1,1"] == "李四"
    assert any("[tblcontent] W-ITEM-FALLBACK t0 2,1: 改写后未过复检" in w
               for w in report)


def test_run_content_value_same_as_original_falls_back(tmp_path):
    tree = _tree()
    client = _FakeClient([_Out({
        "t0 1,1": "张三",  # 值格照抄原值
        "t0 2,1": LONG_REWRITE,
        "p0": "此表服务于年度绩效评估与员工成长跟踪",
    }), _Out({"t0 1,1": "张三"})])
    tables, _, report = run_content([_skeleton()], tree, client,
                                    PromptManager(), tmp_path)
    assert "1,1" not in tables[0]
    assert any("虚构值与原值相同" in w for w in report)


def test_run_content_llm_error_all_fallback(tmp_path):
    class _Boom:
        def structured(self, *a, **kw):
            raise RuntimeError("provider down")

    tree = _tree()
    tables, prose, report = run_content([_skeleton()], tree, _Boom(),
                                        PromptManager(), tmp_path)
    assert tables == {} and prose == {}
    assert len([w for w in report if "LLM 调用失败" in w]) == 3


def test_run_content_no_candidates_zero_llm(tmp_path):
    tree = _tree(prose=("短句",))
    s = TSkeleton(table_title="t", total_cols=1, rows=[
        TRow(cells=[TCell(content="标题", style="header")])])
    tables, prose, report = run_content([s], tree, _NoLLM(),
                                        PromptManager(), tmp_path)
    assert tables == {} and prose == {} and report == []


# ---- 断点续跑 ----

def test_run_content_artifact_resume(tmp_path):
    tree = _tree()
    client = _FakeClient([_Out({
        "t0 1,1": "李四", "t0 2,1": LONG_REWRITE,
        "p0": "此表服务于年度绩效评估与员工成长跟踪",
    })])
    first = run_content([_skeleton()], tree, client, PromptManager(), tmp_path)
    again = run_content([_skeleton()], tree, _NoLLM(), PromptManager(), tmp_path)
    assert first == again


def test_run_content_artifact_invalid_redone(tmp_path):
    tree = _tree()
    (tmp_path / "tblcontent.json").write_text(json.dumps({
        "schema": "tblcontent/1.0",
        "cells": {"t0 2,1": LONG_TEXT},  # 雷同文本：复检不过 → 重做
        "fallbacks": {}, "report": []}), encoding="utf-8")
    client = _FakeClient([_Out({
        "t0 1,1": "李四", "t0 2,1": LONG_REWRITE,
        "p0": "此表服务于年度绩效评估与员工成长跟踪",
    })])
    tables, _, _ = run_content([_skeleton()], tree, client, PromptManager(), tmp_path)
    assert len(client.calls) == 1
    assert tables[0]["2,1"] == LONG_REWRITE_FILLED
