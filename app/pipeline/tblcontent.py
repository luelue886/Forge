"""内容专家：form+PDF 分支第 2 步——骨架格与散文的仿写。

骨架是物理格（无合并展开重复），候选分类比 tablefill 更直接：
- header/label/option 样式格、空格、纯数字格、含 ⟦ 残留格 → 照搬不进 LLM
  （⟦ 残留落 W 报告行）；
- ≥12 汉字当量的 input/note/declare 格 → 述（措辞改写，事实不变）；
- <12 当量、左邻格为字段名词汇（姓名/性别/民族…）且自身非字段名的值格
  → 值（虚构同类型新值，同 C9 语义：数字原样保留、日期类值诚实退格）。

左邻按占位网格解析（跨行 label 的值同样能配对）。掩码与复检沿用
tablefill 语义：格内 ⟦N⟧ 局部占位、unmask 逐个恰一次、ngram+数字溯源、
值≠原值；失败项定向重试一轮，仍败退回照搬落 W 行。散文段（≥12 当量）
并入同一次调用（键 p{i}）。

断点：artifacts/tblcontent.json 存在且逐项复检通过 → 跳过 LLM。
本模块不改骨架——cells 映射由编排层渲染前套用，骨架保持可重校验。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.pipeline.tablefill import (
    MIN_CELL_WEIGHT,
    _despace,
    _is_field_label,
    _pure_numeric,
    cell_passes,
    mask_numbers,
    unmask_numbers,
)
from app.schema.doctree import DocTree
from app.schema.tblskeleton import TCell, TSkeleton, walk_grid
from app.schema.textlen import text_weight

log = logging.getLogger(__name__)

_VERBATIM_STYLES = ("header", "label", "option")


class _CellsOut(BaseModel):
    """内容仿写：项编号 → 改写后全文。"""

    cells: dict[str, str] = Field(default_factory=dict)


def _norm_key(key: str) -> str:
    """LLM 可能把编号写成 "[t0 1,1]" / "t0 1，1"，归一成 "t01,1"。"""
    s = key.strip().strip("[]()（）").replace("，", ",").replace(" ", "")
    return s


def _skeleton_candidates(s: TSkeleton, ti: int,
                         report: list[str]) -> dict[str, tuple[str, str, str]]:
    """骨架物理格 "r,c"（网格坐标）→ (原文, 类型 text|value, 字段名)。"""
    anchors, grid = walk_grid(s)
    out: dict[str, tuple[str, str, str]] = {}
    for (r, c), cell in sorted(anchors.items()):
        text = cell.content
        if not text.strip():
            continue
        if "⟦" in text:
            report.append(f"[tblcontent] W-PLACEHOLDER t{ti} r{r}c{c}: "
                          f"骨架格残留占位符，照搬")
            continue
        if cell.style in _VERBATIM_STYLES or _pure_numeric(text):
            continue
        if text_weight(text) >= MIN_CELL_WEIGHT:
            out[f"{r},{c}"] = (text, "text", "")
            continue
        left = grid.get((r, c - 1))
        if left is not None and _is_field_label(left.content) \
                and not _is_field_label(text):
            out[f"{r},{c}"] = (text, "value", _despace(left.content))
    return out


def _prose_candidates(tree: DocTree) -> dict[str, tuple[str, str, str]]:
    """正文段 ≥12 当量 → 述（文档序，键 p{i}）。"""
    out: dict[str, tuple[str, str, str]] = {}
    i = 0
    for sec in tree.sections:
        for s in sec.walk():
            for b in s.blocks:
                if b.kind in ("para", "list_item") \
                        and text_weight(b.text) >= MIN_CELL_WEIGHT:
                    out[f"p{i}"] = (b.text, "text", "")
                    i += 1
    return out


def _all_candidates(skeletons: list[TSkeleton], tree: DocTree
                    ) -> tuple[dict[str, tuple[str, str, str]], list[str]]:
    report: list[str] = []
    cand: dict[str, tuple[str, str, str]] = {}
    for ti, s in enumerate(skeletons):
        for key, v in _skeleton_candidates(s, ti, report).items():
            cand[f"t{ti} {key}"] = v
    cand.update(_prose_candidates(tree))
    return cand, report


def _round(cand: dict[str, tuple[str, str, str]],
           masked: dict[str, tuple[str, list[str]]], full_text: str,
           client: LLMClient, pm: PromptManager,
           reasons: dict[str, str] | None
           ) -> tuple[dict[str, str], dict[str, str]]:
    """一轮 LLM 调用 → (通过项, 退回项)。reasons 非 None 为重试轮。"""
    keys = sorted(masked) if reasons is None else sorted(reasons)
    entries = []
    for k in keys:
        text, kind, label = cand[k]
        e = {"key": k, "kind": "值" if kind == "value" else "述",
             "label": label, "text": masked[k][0]}
        if reasons is not None:
            e["reason"] = reasons[k]
        entries.append(e)
    tmpl = "tblcontent/retry.j2" if reasons is not None else "tblcontent/user.j2"
    messages = [
        {"role": "system", "content": pm.render("tblcontent/system.md")},
        {"role": "user", "content": pm.render(tmpl, cells=entries)},
    ]
    try:
        out = client.structured(_CellsOut, messages, stage="tblcontent")
    except Exception as e:  # noqa: BLE001 — 本轮调用失败 → 本轮全部照搬
        log.warning("内容专家调用失败，本轮照搬：%s", e)
        return {}, {k: "LLM 调用失败" for k in keys}
    returned = {_norm_key(k): v for k, v in out.cells.items()}
    cells: dict[str, str] = {}
    fallbacks: dict[str, str] = {}
    for k in keys:
        new_text = returned.get(_norm_key(k))
        if new_text is None:
            fallbacks[k] = "未返回该格"
            continue
        restored = unmask_numbers(new_text, masked[k][1])
        if restored is None:
            fallbacks[k] = "占位符回填失败"
        elif cand[k][1] == "value" and restored == cand[k][0]:
            fallbacks[k] = "虚构值与原值相同"
        elif not cell_passes(restored, full_text):
            fallbacks[k] = "改写后未过复检（雷同/数字）"
        else:
            cells[k] = restored
    return cells, fallbacks


def _split(cells: dict[str, str]) -> tuple[dict[int, dict[str, str]],
                                           dict[str, str]]:
    out_tables: dict[int, dict[str, str]] = {}
    out_prose: dict[str, str] = {}
    for k, text in cells.items():
        if k.startswith("t") and " " in k:
            ti_s, rc = k[1:].split(" ", 1)
            out_tables.setdefault(int(ti_s), {})[rc] = text
        else:
            out_prose[k] = text
    return out_tables, out_prose


def _load_artifact(path: Path, skeletons: list[TSkeleton], tree: DocTree
                   ) -> tuple[dict[int, dict[str, str]], dict[str, str],
                              list[str]] | None:
    """断点产物逐项复检；任何失效 → 整体重做。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cells = {str(k): str(v) for k, v in data["cells"].items()}
        report = [str(x) for x in data.get("report", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    cand, _ = _all_candidates(skeletons, tree)
    for k, text in cells.items():
        if k not in cand or not cell_passes(text, tree.full_text):
            return None
        if cand[k][1] == "value" and text == cand[k][0]:
            return None
    out_tables, out_prose = _split(cells)
    return out_tables, out_prose, report


def run_content(skeletons: list[TSkeleton], tree: DocTree, client: LLMClient,
                pm: PromptManager, art_dir: Path
                ) -> tuple[dict[int, dict[str, str]], dict[str, str], list[str]]:
    """骨架格 + 散文仿写（断点安全）。

    返回 (表格改写 {table_index: {"r,c": 新文本}}, 散文改写 {"p{i}": 新文本},
    W 级报告行)。骨架本体不改——映射由编排层渲染前套用。
    """
    art_path = art_dir / "tblcontent.json"
    if art_path.exists():
        got = _load_artifact(art_path, skeletons, tree)
        if got is not None:
            return got

    cand, report = _all_candidates(skeletons, tree)
    cells: dict[str, str] = {}
    fallbacks: dict[str, str] = {}
    if cand:
        masked = {k: mask_numbers(v[0]) for k, v in cand.items()}
        cells, fallbacks = _round(cand, masked, tree.full_text, client, pm, None)
        if fallbacks:
            # 退格前定向重试一轮：只送失败项 + 失败原因，仍败照搬
            again, still = _round(cand, masked, tree.full_text, client, pm,
                                  fallbacks)
            cells.update(again)
            fallbacks = still
    for k, reason in sorted(fallbacks.items()):
        report.append(f"[tblcontent] W-ITEM-FALLBACK {k}: {reason}")
    out_tables, out_prose = _split(cells)
    art_dir.mkdir(parents=True, exist_ok=True)
    art_path.write_text(json.dumps(
        {"schema": "tblcontent/1.0", "cells": cells,
         "fallbacks": fallbacks, "report": report},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return out_tables, out_prose, report
