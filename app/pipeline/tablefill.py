"""表格内容仿写：长描述格措辞改写 + 字段值格虚构，数字 ⟦N⟧ 占位保护。

确定性分类：表头、纯数字格、含 ⟦⟧ 的格（防注入）一律照搬不进 LLM。候选
分两类——≥12 汉字当量的长描述格（措辞改写、事实不变），以及字段名词汇
（姓名/性别/民族/学历…）右邻的短值格（虚构同类型新值，简历去抄袭）。考
核表/评分表之类的维度列不进词汇，天然不受虚构影响；纵向合并 label 下的
值是被换行拆碎的片段，跳过。

候选格内数字全部替换为 ⟦1⟧⟦2⟧… 占位符——数字永不以明文进入 LLM；回填
要求占位符逐个恰现一次、无越界、无新增明文数字，对不上即退回照搬。复检
不过的格先定向重试一轮（只送失败格 + 失败原因），仍败退回照搬，只落 W 级
报告行——排版保真由 deepcopy 承担，内容层有照搬兜底。

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
_PUNCT_END = "。！？；：，、．.!?,;:"

# 字段名词汇（去空格后 fullmatch）：仅个人身份类字段做值虚构——
# 考核维度/数据来源等管理类词不入列，评分表描述不受影响
_FIELD_RE = re.compile("|".join((
    "姓名", "曾用名", "性别", "年龄", "生日", "出生日期", "出生年月",
    "民族", "婚.状况", "婚姻状况", "婚否", "籍贯", "政.面貌", "政治面貌",
    "国籍", "户口", "户籍", "健康状况", "血型", "身高", "体重",
    "邮箱", "e-?mail", "电子邮箱", "电话", "手机", "手机号码", "联系电话",
    "联系方式", "通讯地址", "地址", "住址", "现住址", "家庭住址",
    "现居住地", "居住地", "邮编", "邮政编码",
    "学历", "学位", "专业", "学校", "毕业院校", "毕业学校",
    "求职意向", "兴趣爱好", "特长", "个人特长",
)), re.IGNORECASE)


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
    """候选改写格 "r,c" → 原文（r 为 rows 内 0 基行号，表头不可寻址）。

    python-docx 的 row.cells 按展开语义重复合并格（gridSpan 同行相邻重复、
    vMerge 同列相邻重复）——重复位置只取首个，避免同一物理格产生两份
    互相不一致的改写（渲染游标只写 span 首格，第二份会被静默丢弃）。
    """
    out: dict[str, str] = {}
    prev_row: list[str] = []
    for r, row in enumerate(t.rows):
        prev_cell = ""
        for c, cell in enumerate(row):
            above = prev_row[c] if c < len(prev_row) else ""
            is_merge_dup = bool(cell.strip()) and (cell == prev_cell or cell == above)
            prev_cell = cell
            if is_merge_dup or not cell.strip() or "⟦" in cell:
                continue
            if _pure_numeric(cell):
                continue
            if text_weight(cell) < MIN_CELL_WEIGHT:
                continue
            out[f"{r},{c}"] = cell
        prev_row = list(row)
    return out


def _despace(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _is_field_label(text: str) -> bool:
    return bool(_FIELD_RE.fullmatch(_despace(text)))


def label_value_cells(t: DocTable) -> dict[str, str]:
    """字段值格 "r,c" → 字段名（去空格）。词汇字段作 label、行内右邻为值。

    排除：纯数字/含 ⟦/合并展开重复的值；值本身又是字段名（表头行漏检或
    L,V 错位时保护字段名不被虚构掉）；label 处于纵向合并区或与上一行
    同列同文（合并展开续行）——其"值"是被换行拆碎的片段（考核表实测）。
    值 ≥12 当量不在此列（长描述走事实保留的措辞改写路径）。
    """
    if not t.rows:
        return {}
    # merges 坐标含表头行，换算到 rows 坐标；纵向合并区（rs>1）内的 label 位
    off = 1 if t.header else 0
    span_labels: set[tuple[int, int]] = set()
    if t.merges:
        for m in t.merges:
            if len(m) == 4 and m[2] > 1:
                for ri in range(m[0] - off, m[0] - off + m[2]):
                    span_labels.add((ri, m[1]))

    out: dict[str, str] = {}
    for r, row in enumerate(t.rows):
        prev_row = t.rows[r - 1] if r else None
        prev_cell = ""
        for c, cell in enumerate(row):
            above = prev_row[c] if prev_row and c < len(prev_row) else ""
            is_merge_dup = bool(cell.strip()) and (cell == prev_cell or cell == above)
            prev_cell = cell
            if is_merge_dup or not cell.strip() or "⟦" in cell:
                continue
            if _pure_numeric(cell) or text_weight(cell) >= MIN_CELL_WEIGHT:
                continue
            if c == 0:
                continue
            label = row[c - 1]
            if cell == label or not _is_field_label(label) or _is_field_label(cell):
                continue
            if (r, c - 1) in span_labels:
                continue
            if prev_row is not None and c - 1 < len(prev_row) \
                    and label == prev_row[c - 1]:
                continue
            out[f"{r},{c}"] = _despace(label)
    return out


def rewrite_candidates(t: DocTable) -> dict[str, tuple[str, str, str]]:
    """全部候选格 "r,c" → (原文, 类型, 字段名)。类型 text|value，单表同次调用。

    text（≥12 当量长描述）与 value（<12 当量字段值）当量区间互斥，
    天然无重叠。
    """
    cand: dict[str, tuple[str, str, str]] = {
        key: (text, "text", "") for key, text in candidate_cells(t).items()}
    for key, label in label_value_cells(t).items():
        r, _, c = key.partition(",")
        text = t.rows[int(r)][int(c)]
        cand[key] = (text, "value", label)
    return cand


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
    cand = rewrite_candidates(t)
    for key, text in cells.items():
        if key not in cand or not cell_passes(text, full_text):
            return None
    return cells, fallbacks


def _round(t: DocTable, client: LLMClient, pm: PromptManager,
           cand: dict[str, tuple[str, str, str]],
           masked: dict[str, tuple[str, list[str]]], full_text: str,
           reasons: dict[str, str] | None,
           ) -> tuple[dict[str, str], dict[str, str]]:
    """一轮 LLM 调用 → (通过格, 退格格)。reasons 非 None 为重试轮（只送失败格）。"""
    keys = sorted(masked) if reasons is None else sorted(reasons)
    entries = []
    for k in keys:
        text, kind, label = cand[k]
        e = {"key": k, "kind": "值" if kind == "value" else "述",
             "label": label, "text": masked[k][0]}
        if reasons is not None:
            e["reason"] = reasons[k]
        entries.append(e)
    tmpl = "tablefill/retry.j2" if reasons is not None else "tablefill/user.j2"
    messages = [
        {"role": "system", "content": pm.render("tablefill/system.md")},
        {"role": "user", "content": pm.render(tmpl, cells=entries)},
    ]
    try:
        out = client.structured(_CellsOut, messages, stage=f"tablefill/{t.table_id}")
    except Exception as e:  # noqa: BLE001 — 本轮调用失败 → 本轮全部照搬
        log.warning("表 %s 仿写调用失败，本轮照搬：%s", t.table_id, e)
        return {}, {k: "LLM 调用失败" for k in keys}
    returned = {_norm_key(k): v for k, v in out.cells.items()}
    cells: dict[str, str] = {}
    fallbacks: dict[str, str] = {}
    for k in keys:
        new_text = returned.get(k)
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


def _fill_table(t: DocTable, client: LLMClient, pm: PromptManager,
                tables_dir: Path, full_text: str) -> tuple[dict[str, str], dict[str, str]]:
    path = _artifact_path(tables_dir, t.table_id)
    if path.exists():
        got = _load_artifact(path, t, full_text)
        if got is not None:
            return got

    cand = rewrite_candidates(t)
    cells: dict[str, str] = {}
    fallbacks: dict[str, str] = {}
    if cand:
        masked = {k: mask_numbers(v[0]) for k, v in cand.items()}
        cells, fallbacks = _round(t, client, pm, cand, masked, full_text, None)
        if fallbacks:
            # 退格前定向重试一轮：只送失败格 + 失败原因，仍败照搬
            again, still = _round(t, client, pm, cand, masked, full_text,
                                  fallbacks)
            cells.update(again)
            fallbacks = still
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
