"""表格内容仿写：≥12 汉字当量的长文本格送 LLM 改写，数字 ⟦N⟧ 占位保护。

确定性分类：表头、纯数字格、短格（<12 当量）、含 ⟦⟧ 的格（防注入）一律
照搬不进 LLM。候选格内数字全部替换为 ⟦1⟧⟦2⟧… 占位符——数字永不以明文
进入 LLM；回填要求占位符逐个恰现一次、无越界、无新增明文数字，对不上
即退回照搬。改写格复检（ngram + 数字溯源）不过也退回照搬，只落 W 级
报告行——不进 QA 重试循环：排版保真由 C3 deepcopy 承担，内容层有照搬
兜底，无需多轮。

断点：每表落 artifacts/tables/<table_id>.json（含 cells 可为空）；存在
且逐格复检通过则跳过，任何一格失效则整表重做。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.qa.ngram import ngram_hits
from app.qa.numbers import (
    check_numbers,
    extract_number_tokens,
    number_spans,
    strip_number_tokens,
)
from app.schema.doctree import DocTable, DocTree
from app.schema.textlen import text_weight

log = logging.getLogger(__name__)

MIN_CELL_WEIGHT = 12  # 候选格下限（汉字当量）
_PLACEHOLDER = re.compile(r"⟦(\d+)⟧")


class _CellsOut(BaseModel):
    """表格仿写：单元格编号 "r,c" → 改写后整格文本。"""

    cells: dict[str, str] = Field(default_factory=dict)


def mask_numbers(text: str) -> tuple[str, list[str]]:
    """明文数字 → ⟦1⟧⟦2⟧…（按出现顺序），返回 (掩码文本, 原文 token 列表)。"""
    tokens: list[str] = []
    out: list[str] = []
    last = 0
    for i, (s, e) in enumerate(number_spans(text), start=1):
        out.append(text[last:s])
        out.append(f"⟦{i}⟧")
        tokens.append(text[s:e])
        last = e
    out.append(text[last:])
    return "".join(out), tokens


def unmask_numbers(masked_result: str, tokens: list[str]) -> str | None:
    """LLM 改写结果回填；任何违规返回 None（调用方退回照搬）。"""
    bare = _PLACEHOLDER.sub("〇", masked_result)
    if extract_number_tokens(bare) or "⟦" in bare or "⟧" in bare:
        return None  # 新增明文数字 / 非法占位符
    counts: dict[int, int] = {}
    for m in _PLACEHOLDER.finditer(masked_result):
        counts[int(m.group(1))] = counts.get(int(m.group(1)), 0) + 1
    if sorted(counts) != list(range(1, len(tokens) + 1)):
        return None  # 越界 / 缺失
    if any(v != 1 for v in counts.values()):
        return None  # 重复
    return _PLACEHOLDER.sub(lambda m: tokens[int(m.group(1)) - 1], masked_result)


def _pure_numeric(text: str) -> bool:
    """全部信息量都在数字 token 上（"35" / "43.8%"）→ 照搬。"""
    return text_weight(strip_number_tokens(text)) == 0


def candidate_cells(t: DocTable) -> dict[str, str]:
    """候选改写格 "r,c" → 原文（r 为 rows 内 0 基行号，表头不可寻址）。"""
    out: dict[str, str] = {}
    for r, row in enumerate(t.rows):
        for c, cell in enumerate(row):
            if not cell.strip() or "⟦" in cell:
                continue
            if _pure_numeric(cell):
                continue
            if text_weight(cell) < MIN_CELL_WEIGHT:
                continue
            out[f"{r},{c}"] = cell
    return out


def cell_passes(new_text: str, full_text: str) -> bool:
    return not ngram_hits(new_text, full_text) and not check_numbers(new_text, full_text)


def _norm_key(key: str) -> str:
    """LLM 可能把编号写成 "[2,3]" / "2，3"，归一成 "2,3"。"""
    s = key.strip().strip("[]()（）").replace("，", ",").replace(" ", "")
    return s


def _artifact_path(tables_dir: Path, table_id: str) -> Path:
    return tables_dir / f"{table_id}.json"


def _load_artifact(path: Path, t: DocTable,
                   full_text: str) -> tuple[dict[str, str], dict[str, str]] | None:
    """断点产物逐格复检；任何失效 → 整表重做。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cells = {str(k): str(v) for k, v in data["cells"].items()}
        fallbacks = {str(k): str(v) for k, v in data.get("fallbacks", {}).items()}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    cand = candidate_cells(t)
    for key, text in cells.items():
        if key not in cand or not cell_passes(text, full_text):
            return None
    return cells, fallbacks


def _fill_table(t: DocTable, client: LLMClient, pm: PromptManager,
                tables_dir: Path, full_text: str) -> tuple[dict[str, str], dict[str, str]]:
    path = _artifact_path(tables_dir, t.table_id)
    if path.exists():
        got = _load_artifact(path, t, full_text)
        if got is not None:
            return got

    cand = candidate_cells(t)
    cells: dict[str, str] = {}
    fallbacks: dict[str, str] = {}
    if cand:
        masked = {key: mask_numbers(text) for key, text in cand.items()}
        user = pm.render(
            "tablefill/user.j2",
            cells=[{"key": k, "text": m[0]} for k, m in sorted(masked.items())])
        messages = [
            {"role": "system", "content": pm.render("tablefill/system.md")},
            {"role": "user", "content": user},
        ]
        out = None
        try:
            out = client.structured(_CellsOut, messages, stage=f"tablefill/{t.table_id}")
        except Exception as e:  # noqa: BLE001 — 整表调用失败 → 全部照搬
            log.warning("表 %s 仿写调用失败，整表照搬：%s", t.table_id, e)
        if out is not None:
            returned = {_norm_key(k): v for k, v in out.cells.items()}
            for key, (_masked_text, tokens) in masked.items():
                new_text = returned.get(key)
                if new_text is None:
                    fallbacks[key] = "未返回该格"
                    continue
                restored = unmask_numbers(new_text, tokens)
                if restored is None:
                    fallbacks[key] = "占位符回填失败"
                elif not cell_passes(restored, full_text):
                    fallbacks[key] = "改写后未过复检（雷同/数字）"
                else:
                    cells[key] = restored
        else:
            fallbacks = {k: "LLM 调用失败" for k in cand}
    path.write_text(json.dumps(
        {"table_id": t.table_id, "cells": cells, "fallbacks": fallbacks},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return cells, fallbacks


def fill_all_tables(tree: DocTree, client: LLMClient, pm: PromptManager,
                    tables_dir: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """全表仿写（断点安全）。返回 (table_id → {"r,c": 改写文本}, W 级报告行)。"""
    tables_dir.mkdir(parents=True, exist_ok=True)
    rewrites: dict[str, dict[str, str]] = {}
    report: list[str] = []
    for t in tree.tables:
        cells, fallbacks = _fill_table(t, client, pm, tables_dir, tree.full_text)
        rewrites[t.table_id] = cells
        for key, reason in sorted(fallbacks.items()):
            report.append(f"[tablefill] W-CELL-FALLBACK {t.table_id} {key}: {reason}")
    return rewrites, report
