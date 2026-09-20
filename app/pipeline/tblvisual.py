"""视觉总监：骨架 → 列宽% + 行高 pt（form+PDF 分支第 3 步）。

LLM 看不到文档几何（PDF 碎片几何本身不可信），只看代码算好的逐列内容
当量统计与版心宽——标签列窄而稳、内容列按当量、书写行行高给足。
validate_visual 不过 → 1 次定向重试 → 仍败确定性兜底（源碎片列宽可用
则用，否则内容当量加权；<4% 抬到 4% 从最宽列扣减，和恒 100）。

断点：artifacts/tblvisual.json 存在且逐表复检通过 → 跳过 LLM。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.schema.doctree import DocTree
from app.schema.tblskeleton import (
    MIN_COL_PERCENT,
    TSkeleton,
    TVisualTable,
    VisualOut,
    renormalize_widths,
    validate_visual,
    walk_grid,
)
from app.schema.textlen import text_weight

log = logging.getLogger(__name__)

CONTENT_WIDTH_CM = 14.64  # A4 21.0 − 3.18×2 版心宽


def _col_weights(s: TSkeleton) -> list[float]:
    """逐列内容当量（格当量摊到各占位列）。"""
    weights = [0.0] * s.total_cols
    anchors, _ = walk_grid(s)
    for (r, c), cell in anchors.items():
        w = text_weight(cell.content)
        for dc in range(cell.colspan):
            if c + dc < s.total_cols:
                weights[c + dc] += w / cell.colspan
    return weights


def _writing_rows(s: TSkeleton) -> list[int]:
    """含空 input/note 格的行（书写区，行高须给足）。"""
    return [r for r, row in enumerate(s.rows)
            if any(cell.style in ("input", "note")
                   and not cell.content.strip() for cell in row.cells)]


def _table_stats(ti: int, s: TSkeleton) -> str:
    weights = _col_weights(s)
    cols = ", ".join(f"c{i}={weights[i]:.0f}" for i in range(s.total_cols))
    styles = sorted({cell.style for row in s.rows for cell in row.cells
                     if cell.content.strip()})
    line = (f"表{ti}《{s.table_title or '无题'}》：{s.total_cols}列×"
            f"{len(s.rows)}行；列内容当量 {cols}；含样式 {styles}")
    wr = _writing_rows(s)
    if wr:
        line += f"；书写行 {wr}（input/note 空行，行高给足）"
    return line


def _src_widths(s: TSkeleton, tree: DocTree) -> list[float] | None:
    tables = {t.table_id: t for t in tree.tables}
    for tid in s.src_tables:
        t = tables.get(tid)
        if t is not None and t.col_widths \
                and len(t.col_widths) == s.total_cols:
            return t.col_widths
    return None


def _clamp_min(widths: list[int]) -> list[int]:
    """<4% 的列抬到 4%，从最宽列扣减，保持和为 100。"""
    w = list(widths)
    for i in range(len(w)):
        if w[i] < MIN_COL_PERCENT:
            w[i] = MIN_COL_PERCENT
    diff = sum(w) - 100
    while diff > 0:
        i = max(range(len(w)), key=lambda k: w[k])
        take = min(diff, w[i] - MIN_COL_PERCENT)
        if take <= 0:
            break
        w[i] -= take
        diff -= take
    return w


def _fallback_visual(ti: int, s: TSkeleton, tree: DocTree) -> TVisualTable:
    src = _src_widths(s, tree)
    if src:
        widths = renormalize_widths(
            [max(MIN_COL_PERCENT, round(w * 100)) for w in src])
    else:
        weights = _col_weights(s)
        total = sum(weights) or 1.0
        widths = renormalize_widths(
            [max(MIN_COL_PERCENT, round(w / total * 100)) for w in weights])
    return TVisualTable(table_index=ti, col_widths=_clamp_min(widths))


def _check_visual(out: VisualOut, skeletons: list[TSkeleton]) -> list[str]:
    if len(out.tables) != len(skeletons):
        return [f"表数量 {len(out.tables)} ≠ 骨架数 {len(skeletons)}"]
    by_index = {v.table_index: v for v in out.tables}
    if set(by_index) != set(range(len(skeletons))):
        return [f"table_index 不完整：{sorted(by_index)}"]
    errors: list[str] = []
    for ti, s in enumerate(skeletons):
        errors += validate_visual(by_index[ti], s.total_cols, len(s.rows))
    return errors


def _load_artifact(path: Path, skeletons: list[TSkeleton]
                   ) -> tuple[list[TVisualTable], list[str]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        loaded = [TVisualTable.model_validate(t) for t in data["tables"]]
        report = [str(x) for x in data.get("report", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    by_index = {v.table_index: v for v in loaded}
    if set(by_index) != set(range(len(skeletons))):
        return None
    visuals = [by_index[ti] for ti in range(len(skeletons))]
    errors = [e for ti, s in enumerate(skeletons)
              for e in validate_visual(visuals[ti], s.total_cols, len(s.rows))]
    if errors:
        log.info("tblvisual 断点产物校验失败，重做：%s", errors[:3])
        return None
    return visuals, report


def run_visual(skeletons: list[TSkeleton], tree: DocTree, client: LLMClient,
               pm: PromptManager, art_dir: Path
               ) -> tuple[list[TVisualTable], list[str]]:
    """骨架 → 视觉参数（断点安全）。返回 (逐表视觉参数, W 级报告行)。"""
    art_path = art_dir / "tblvisual.json"
    if art_path.exists():
        got = _load_artifact(art_path, skeletons)
        if got is not None:
            return got

    visuals: list[TVisualTable] = []
    report: list[str] = []
    source = "llm"
    stats = [_table_stats(ti, s) for ti, s in enumerate(skeletons)]
    user_ctx = pm.render("tblvisual/user.j2", tables=stats,
                         content_width_cm=CONTENT_WIDTH_CM)
    system = pm.render("tblvisual/system.md")
    out: VisualOut | None = None
    errors: list[str] = []
    for attempt in (1, 2):
        if attempt == 1:
            user = user_ctx
        else:
            user = pm.render("tblvisual/retry.j2", errors=errors[:10],
                             context=user_ctx)
        try:
            out = client.structured(
                VisualOut,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                stage=f"tblvisual#a{attempt}")
        except Exception as e:  # noqa: BLE001 — LLM 不可用 → 兜底
            log.warning("视觉总监调用失败，走确定性兜底：%s", e)
            out = None
            errors = [f"LLM 调用失败：{e}"]
            break
        errors = _check_visual(out, skeletons)
        if not errors:
            break
    if out is not None and not errors:
        by_index = {v.table_index: v for v in out.tables}
        visuals = [
            TVisualTable(
                table_index=ti, col_widths=renormalize_widths(by_index[ti].col_widths),
                row_heights=by_index[ti].row_heights)
            for ti in range(len(skeletons))]
    else:
        source = "fallback"
        reason = errors[0][:60] if errors else "未知"
        for ti, s in enumerate(skeletons):
            visuals.append(_fallback_visual(ti, s, tree))
            report.append(f"[tblvisual] W-VISUAL-FALLBACK t{ti}: {reason}（确定性兜底列宽）")

    art_dir.mkdir(parents=True, exist_ok=True)
    art_path.write_text(json.dumps(
        {"schema": "tblvisual/1.0", "source": source,
         "tables": [v.model_dump() for v in visuals], "report": report},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return visuals, report
