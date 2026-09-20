from __future__ import annotations

import json

import pytest

from app.llm.prompts import PromptManager
from app.pipeline.tblarch import (
    FormBranchFallback,
    build_mask_pool,
    run_architect,
)
from app.schema.doctree import DocBlock, DocMeta, DocSection, DocTable, DocTree
from app.schema.docplan import DocPlan
from app.schema.enums import Genre
from app.schema.tblskeleton import (
    TCell,
    TRow,
    TSkeleton,
    TVisualTable,
    coverage_missing,
    renormalize_widths,
    validate_skeleton,
    validate_visual,
)


def _tree(tables: list[DocTable], prose=("绩效考核表",)) -> DocTree:
    root = DocSection(section_id="sec-0000", level=0, title="表")
    root.blocks = [DocBlock(block_id=f"blk-{i:04d}", kind="para", text=p)
                   for i, p in enumerate(prose)]
    full = " ".join(prose)
    return DocTree(
        meta=DocMeta(source_format="pdf", source_name="f.pdf", n_chars=len(full)),
        sections=[root], tables=tables, full_text=full)


def _plan(title="人事考核表") -> DocPlan:
    return DocPlan(genre=Genre.FORM, title=title, items=[])


def _sk(title: str, cols: int,
        rows_spec: list[list[tuple[str, int, int, str]]]) -> TSkeleton:
    return TSkeleton(
        table_title=title, total_cols=cols,
        rows=[TRow(cells=[TCell(content=c, colspan=cs, rowspan=rs, style=st)
                          for (c, cs, rs, st) in row]) for row in rows_spec])


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


class _NoLLM:
    def structured(self, *a, **kw):
        raise AssertionError("不应调用 LLM")


# ---- validate_skeleton：占位网格游标走格 ----

def test_validate_skeleton_ok_with_rowspan():
    s = _sk("t", 3, [
        [("姓名", 1, 1, "label"), ("", 2, 1, "input")],
        [("基本情况", 1, 2, "label"), ("", 2, 1, "input")],
        [("", 2, 1, "input")],  # 左列被上方 rowspan=2 占据，不再重复
    ])
    assert validate_skeleton(s) == []


def test_validate_skeleton_hole():
    s = _sk("t", 2, [[("姓名", 1, 1, "label")]])  # 行宽不齐 → 空洞
    assert any("空洞" in e for e in validate_skeleton(s))


def test_validate_skeleton_colspan_overflow():
    s = _sk("t", 2, [[("A", 3, 1, "input")]])
    assert any("越界" in e for e in validate_skeleton(s))


def test_validate_skeleton_rowspan_overlap():
    # C 跨 2 列，右半撞上 B 的 rowspan=2 占位
    s = _sk("t", 3, [
        [("A", 1, 1, "label"), ("B", 2, 2, "label")],
        [("C", 2, 1, "input")],
    ])
    assert any("重叠" in e for e in validate_skeleton(s))


def test_validate_skeleton_rowspan_overflow():
    s = _sk("t", 1, [[("基本", 1, 2, "label")]])
    assert any("rowspan" in e for e in validate_skeleton(s))


def test_validate_skeleton_degenerate():
    assert validate_skeleton(TSkeleton(table_title="t", total_cols=0)) != []
    assert validate_skeleton(TSkeleton(table_title="t", total_cols=2, rows=[])) != []
    s = _sk("t", 2, [[]])  # 空行
    assert any("没有任何格子" in e for e in validate_skeleton(s))
    s = _sk("t", 2, [[("A", 0, 1, "input"), ("B", 2, 1, "input")]])
    assert any("colspan/rowspan <1" in e for e in validate_skeleton(s))


def test_validate_skeleton_oversize():
    s = _sk("t", 100, [[("x", 100, 1, "input")] for _ in range(25)])
    assert any("超限" in e for e in validate_skeleton(s))


# ---- validate_visual / renormalize ----

def test_validate_visual_ok():
    v = TVisualTable(table_index=0, col_widths=[20, 30, 50], row_heights=[20, 30])
    assert validate_visual(v, 3, 2) == []
    v2 = TVisualTable(table_index=1, col_widths=[50, 50])
    assert validate_visual(v2, 2, 2) == []


def test_validate_visual_errors():
    v = TVisualTable(table_index=0, col_widths=[20, 30], row_heights=[10, 999])
    errs = validate_visual(v, 3, 2)
    assert any("数量" in e for e in errs)
    assert any("总和" in e for e in errs)
    assert any("行高" in e for e in errs)
    v = TVisualTable(table_index=0, col_widths=[3, 97])
    assert any("<4%" in e for e in validate_visual(v, 2, 1))


def test_renormalize_widths():
    assert sum(renormalize_widths([33, 33, 33])) == 100
    assert sum(renormalize_widths([20, 30, 50])) == 100
    w = renormalize_widths([10, 10, 10, 70])
    assert sum(w) == 100 and all(x >= 4 for x in w)


# ---- build_mask_pool：去重 + 全局 id ----

def test_build_mask_pool_dedup_and_global_ids():
    t = DocTable(table_id="tbl-001", section_id="s", n_rows=2, n_cols=2,
                 header=["姓名", "电话"],
                 rows=[["张三", "13800000000"], ["李四", "13800000000"]])
    tree = _tree([t], prose=("2026 年度考核",))
    pool = build_mask_pool(tree)
    # 重复文本去重；数字全局递增 id：表格数字先于正文数字
    assert pool.texts["13800000000"] == "⟦1⟧"
    assert pool.values == {1: "13800000000", 2: "2026"}
    assert pool.prose_masked == ["⟦2⟧ 年度考核"]
    assert "张三" in pool.texts and pool.texts["张三"] == "张三"


# ---- coverage_missing ----

def _cov_tree():
    t = DocTable(table_id="tbl-001", section_id="s", n_rows=1, n_cols=2,
                 header=["项目", "说明"], rows=[["安全生产费用", "占预算 5%"]])
    return _tree([t])


def test_coverage_all_present():
    pool = build_mask_pool(_cov_tree())
    s = _sk("t", 2, [
        [("项目", 1, 1, "header"), ("说明", 1, 1, "header")],
        [("安全生产费用", 1, 1, "label"), (pool.texts["占预算5%"], 1, 1, "input")],
    ])
    assert coverage_missing([s], pool) == []


def test_coverage_missing_text_uses_masked_in_error():
    pool = build_mask_pool(_cov_tree())
    s = _sk("t", 2, [[("项目", 1, 1, "header"), ("说明", 1, 1, "header")]])
    errs = coverage_missing([s], pool)
    assert any("未进骨架" in e for e in errs)
    # 错误信息回传 LLM 时不得泄露明文数字：5% 必须以掩码形态出现
    assert not any("5%" in e for e in errs)


def test_coverage_unknown_placeholder():
    pool = build_mask_pool(_cov_tree())
    s = _sk("t", 1, [[("⟦99⟧", 1, 1, "input")]])
    assert any("未知占位符" in e for e in coverage_missing([s], pool))


def test_coverage_noise_exempt():
    pool = build_mask_pool(_cov_tree())
    s = _sk("t", 2, [
        [("项目", 1, 1, "header"), ("说明", 1, 1, "header")],
        [("安全生产费用", 1, 1, "label"), (pool.texts["占预算5%"], 1, 1, "input")],
    ])
    pool.texts["勤"] = "勤"  # 单字符噪声：豁免覆盖检查
    assert coverage_missing([s], pool) == []


# ---- run_architect：LLM 编排 + 确定性回填 ----

def _fee_tree() -> DocTree:
    main = DocTable(table_id="tbl-001", section_id="s", n_rows=1, n_cols=2,
                    header=["项目", "金额"], rows=[["安全生产费用", "5000"]])
    vertical = DocTable(table_id="tbl-002", section_id="s", n_rows=3, n_cols=1,
                        rows=[["勤"], ["情"], ["况"]])
    return _tree([main, vertical])


def _fee_skeleton(missing: str | None = None, extra: str = "") -> TSkeleton:
    header_cells = [TCell(content="项目", style="header"), TCell(content="金额", style="header")]
    if missing == "金额":
        header_cells[1] = TCell(content="", style="header")
    return TSkeleton(
        table_title="费用表", total_cols=2, src_tables=["tbl-001", "tbl-002"],
        rows=[
            TRow(cells=header_cells),
            TRow(cells=[TCell(content="勤情况", rowspan=2, style="label"),
                        TCell(content="安全生产费用" + extra)]),
            TRow(cells=[TCell(content="⟦1⟧")]),
        ])


def test_run_architect_rebuild_and_masking(tmp_path):
    tree = _fee_tree()
    client = _FakeClient([type("Out", (), {"tables": [_fee_skeleton()],
                                           "notes": ""})()])
    tables, pool, report = run_architect(tree, _plan(), client,
                                         PromptManager(), tmp_path)
    assert len(tables) == 1
    s = tables[0]
    # 竖排碎片按序拼接、行宽占位正确；确定性回填：⟦1⟧ → 原值 5000
    assert s.rows[1].cells[0].content == "勤情况"
    assert s.rows[2].cells[0].content == "5000"
    assert s.rows[1].cells[1].content == "安全生产费用"
    assert report == []
    # 掩码铁律：prompt 中不得出现明文数字
    user_msg = client.calls[0][1][1]["content"]
    assert "5000" not in user_msg and "⟦1⟧" in user_msg and "勤" in user_msg
    # 断点产物落盘
    art = json.loads((tmp_path / "tblarch.json").read_text(encoding="utf-8"))
    assert art["schema"] == "tblarch/1.0" and len(art["tables"]) == 1


def test_run_architect_linewrap_concat(tmp_path):
    t1 = DocTable(table_id="tbl-001", section_id="s", n_rows=1, n_cols=1,
                  header=["服务对象"], rows=[["和内部客户，内部"]])
    t2 = DocTable(table_id="tbl-002", section_id="s", n_rows=1, n_cols=1,
                  rows=[["客户包括公司各部"]])
    t3 = DocTable(table_id="tbl-003", section_id="s", n_rows=1, n_cols=1,
                  rows=[["门"]])
    tree = _tree([t1, t2, t3])
    sk = TSkeleton(table_title="服务", total_cols=2, src_tables=["tbl-001"],
                   rows=[TRow(cells=[
                       TCell(content="服务对象", style="header"),
                       TCell(content="和内部客户，内部客户包括公司各部门")])])
    client = _FakeClient([type("Out", (), {"tables": [sk], "notes": ""})()])
    tables, _, report = run_architect(tree, _plan(), client,
                                      PromptManager(), tmp_path)
    assert tables[0].rows[0].cells[1].content == "和内部客户，内部客户包括公司各部门"
    assert report == []


def test_run_architect_unmatched_extra_text_reports_w(tmp_path):
    tree = _fee_tree()
    client = _FakeClient([type("Out", (), {
        "tables": [_fee_skeleton(extra="总计")], "notes": ""})()])
    tables, _, report = run_architect(tree, _plan(), client,
                                      PromptManager(), tmp_path)
    # 池外多写的"总计"：回声保留 + W 报告行（覆盖检查仍通过）
    assert tables[0].rows[1].cells[1].content == "安全生产费用总计"
    assert report == ["[tblarch] W-UNMATCHED t0 r1c1: 骨架格含源池外文本，保留架构师回声"]


def test_run_architect_retry_with_errors_then_pass(tmp_path):
    tree = _fee_tree()
    bad = type("Out", (), {"tables": [_fee_skeleton(missing="金额")],
                           "notes": ""})()
    good = type("Out", (), {"tables": [_fee_skeleton()], "notes": ""})()
    client = _FakeClient([bad, good])
    tables, _, _ = run_architect(tree, _plan(), client, PromptManager(), tmp_path)
    assert len(client.calls) == 2
    # 重试轮只送错误清单 + 原始输入，错误信息为掩码态
    retry_user = client.calls[1][1][1]["content"]
    assert "未进骨架" in retry_user and "金额" in retry_user
    assert tables[0].rows[0].cells[1].content == "金额"


def test_run_architect_unknown_placeholder_retry(tmp_path):
    tree = _fee_tree()
    sk = _fee_skeleton()
    sk.rows[2].cells[0].content = "⟦9⟧"  # 池外 id
    good = type("Out", (), {"tables": [_fee_skeleton()], "notes": ""})()
    client = _FakeClient([type("Out", (), {"tables": [sk], "notes": ""})(), good])
    tables, _, _ = run_architect(tree, _plan(), client, PromptManager(), tmp_path)
    assert "未知占位符 ⟦9⟧" in client.calls[1][1][1]["content"]
    assert tables[0].rows[2].cells[0].content == "5000"


def test_run_architect_two_rounds_fail_falls_back(tmp_path):
    tree = _fee_tree()
    bad = type("Out", (), {"tables": [_fee_skeleton(missing="金额")],
                           "notes": ""})()
    client = _FakeClient([bad, bad])
    with pytest.raises(FormBranchFallback, match="两轮未过校验"):
        run_architect(tree, _plan(), client, PromptManager(), tmp_path)


def test_run_architect_llm_error_falls_back(tmp_path):
    class _Boom:
        def structured(self, *a, **kw):
            raise RuntimeError("provider down")

    with pytest.raises(FormBranchFallback, match="LLM 调用失败"):
        run_architect(_fee_tree(), _plan(), _Boom(), PromptManager(), tmp_path)


def test_run_architect_empty_pool_falls_back(tmp_path):
    t = DocTable(table_id="tbl-001", section_id="s", n_rows=1, n_cols=2,
                 rows=[["", ""]])
    with pytest.raises(FormBranchFallback, match="无任何格文本"):
        run_architect(_tree([t], prose=()), _plan(), _NoLLM(),
                      PromptManager(), tmp_path)


def test_run_architect_artifact_resume_no_llm(tmp_path):
    tree = _fee_tree()
    client = _FakeClient([type("Out", (), {"tables": [_fee_skeleton()],
                                           "notes": ""})()])
    first = run_architect(tree, _plan(), client, PromptManager(), tmp_path)
    again = run_architect(tree, _plan(), _NoLLM(), PromptManager(), tmp_path)
    assert [s.model_dump() for s in first[0]] == [s.model_dump() for s in again[0]]
    assert first[2] == again[2]


def test_run_architect_artifact_invalid_redone(tmp_path):
    tree = _fee_tree()
    (tmp_path / "tblarch.json").write_text(
        json.dumps({"schema": "tblarch/1.0", "tables": [], "report": []}),
        encoding="utf-8")  # 空骨架：校验失败（覆盖不过）→ 重做
    client = _FakeClient([type("Out", (), {"tables": [_fee_skeleton()],
                                           "notes": ""})()])
    tables, _, _ = run_architect(tree, _plan(), client, PromptManager(), tmp_path)
    assert len(client.calls) == 1 and len(tables) == 1
