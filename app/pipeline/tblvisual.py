"""视觉总监：骨架 → 列宽% + 行高 pt（form+PDF 分支第 3 步）。

列宽由代码确定性计算（F1）：源表列宽比例优先（DocTable.col_widths 按
src_tables 映射 + 列数匹配），先验经可用性审计——短标签列远宽于书写列
的语义反转只搬反转对（3:1 封顶），单列垄断弃用走内容当量 + 空输入列
占位当量（占位随行内标签长度缩放——长问长答，防书写区被挤没）；照片格
跨列钳制标准证件照最小宽。LLM 只负责行高（书写行给足、表头紧凑）；照片
格跨行行高合计钳制标准证件照最小高（F2）。抽检意见重跑轮锚定旧参数最小
修改，事后照片钳制兜底。LLM 失败 → 确定性基准列宽（行高不指定）。

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
    ROW_HEIGHT_RANGE,
    TSkeleton,
    TVisualTable,
    VisualOut,
    photo_size_cm,
    renormalize_widths,
    validate_visual,
    walk_grid,
)
from app.schema.textlen import text_weight

log = logging.getLogger(__name__)

CONTENT_WIDTH_CM = 14.64  # A4 21.0 − 3.18×2 版心宽
_INPUT_PLACEHOLDER_WEIGHT = 5.0  # 空输入列占位当量（约 5 字书写空间）
_ANSWER_LABEL_RATIO = 0.8  # 占位随行内标签缩放：答案长度 ≈ 问题长度 × 0.8
_LABEL_MAX_WEIGHT = 6.0  # 表单形态下标签列宽度需求封顶（标签可换行）


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


def _table_stats(ti: int, s: TSkeleton,
                 base: list[int] | None = None) -> str:
    weights = _col_weights(s)
    cols = ", ".join(f"c{i}={weights[i]:.0f}" for i in range(s.total_cols))
    styles = sorted({cell.style for row in s.rows for cell in row.cells
                     if cell.content.strip()})
    line = (f"表{ti}《{s.table_title or '无题'}》：{s.total_cols}列×"
            f"{len(s.rows)}行；列内容当量 {cols}；含样式 {styles}")
    if base is not None:
        line += f"；基准列宽 {base}"
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


def _content_weights(s: TSkeleton) -> list[float]:
    """内容当量 + 表单形态修正：标签列封顶（可换行），空输入列保占位。

    占位当量随行内标签长度缩放（长问长答）：问答带里 20 字问题的答案
    书写区不该只有 5 字占位——按标签当量 ×0.8 抬升，书写区才能拿回主宽。
    """
    weights = _col_weights(s)
    anchors, _ = walk_grid(s)
    row_label: dict[int, float] = {}
    col_label: dict[int, float] = {}
    col_input: dict[int, float] = {}
    for (r, c), cell in anchors.items():
        if cell.content.strip() and cell.style in ("label", "header"):
            w = text_weight(cell.content)
            row_label[r] = max(row_label.get(r, 0.0), w)
            col_label[c] = max(col_label.get(c, 0.0), w)
        elif cell.style in ("input", "note") and not cell.content.strip():
            col_input[c] = max(col_input.get(c, 0.0),
                               _INPUT_PLACEHOLDER_WEIGHT,
                               _ANSWER_LABEL_RATIO * row_label.get(r, 0.0))
    if not col_input:
        return weights
    for c, need in col_input.items():
        weights[c] = max(weights[c], need)
    for c, w in col_label.items():
        if c not in col_input:
            weights[c] = min(weights[c], _LABEL_MAX_WEIGHT)
    return weights


def _monopoly(src: list[float]) -> bool:
    """单列垄断：多列表某列 >90%（书写列 4% 连一个字都放不下）。"""
    return len(src) >= 2 and max(src) > 0.90


def _repair_prior(s: TSkeleton,
                  src: list[float]) -> tuple[list[float], int]:
    """短标签列宽度搬回书写列（3:1 封顶）。返回 (修复后先验, 修复对数)。

    实录（干部履历表原生 docx）：姓名列 29% 配书写列 6%——tblGrid 是
    label-inflated 死数据，照抄即复刻"字段宽度大于需填内容宽度"。只搬
    超出 3 倍的部分（幅度天然自限，golden 样例偶发小幅失衡不受影响），
    其余列照抄——真实版面（Word 转换重写 grid）本就无此反转，不动。
    """
    fixed = list(src)
    anchors, _ = walk_grid(s)
    pairs: set[tuple[int, int]] = set()
    for (r, c), cell in anchors.items():
        if not (cell.content.strip() and cell.style in ("label", "header")):
            continue
        if text_weight(cell.content) > _LABEL_MAX_WEIGHT:
            continue
        for b in (c - 1, c + 1):
            nb = anchors.get((r, b))
            if (nb is not None and 0 <= b < len(src)
                    and nb.style in ("input", "note")
                    and not nb.content.strip() and src[c] > 3 * src[b]):
                pairs.add((c, b))
    for a, b in sorted(pairs):
        excess = src[a] - 3 * src[b]
        fixed[a] -= excess
        fixed[b] += excess
    return fixed, len(pairs)


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


def _photo_min_widths(widths: list[int], s: TSkeleton) -> list[int]:
    """照片格跨列合计 ≥ 标准证件照宽：差额加到末跨列，从最宽非跨列扣减。"""
    w = list(widths)
    anchors, _ = walk_grid(s)
    for (r, c), cell in anchors.items():
        size = photo_size_cm(cell.content)
        if size is None:
            continue
        span = range(c, min(c + cell.colspan, s.total_cols))
        need = round(size[0] / CONTENT_WIDTH_CM * 100)
        deficit = need - sum(w[i] for i in span)
        if deficit <= 0:
            continue
        w[span.stop - 1] += deficit
        donors = [i for i in range(s.total_cols) if i not in span]
        for i in sorted(donors, key=lambda k: -w[k]):
            if deficit <= 0:
                break
            take = min(deficit, w[i] - MIN_COL_PERCENT)
            w[i] -= take
            deficit -= take
    return w


def enforce_photo_widths(widths: list[int], s: TSkeleton) -> list[int]:
    """照片最小宽钳制 + 归一（最小值语义，幂等，不与抽检意见震荡）。"""
    return renormalize_widths(_photo_min_widths(list(widths), s))


def enforce_photo_heights(heights: list[int], s: TSkeleton) -> list[int]:
    """照片格跨行行高合计 ≥ 标准证件照高：差额加到跨行末行（幂等钳制）。"""
    h = list(heights)
    anchors, _ = walk_grid(s)
    for (r, c), cell in anchors.items():
        size = photo_size_cm(cell.content)
        if size is None:
            continue
        span = range(r, min(r + cell.rowspan, len(h)))
        need = round(size[1] * 28.35)  # cm → pt
        deficit = need - sum(h[i] for i in span)
        if deficit > 0:
            h[span.stop - 1] = min(ROW_HEIGHT_RANGE[1],
                                   h[span.stop - 1] + deficit)
    return h


def _base_widths(s: TSkeleton, tree: DocTree) -> list[int]:
    """确定性列宽基准：源表比例优先（短标签反转修复、垄断弃用），内容
    当量+占位兜底，照片最小宽钳制。"""
    src = _src_widths(s, tree)
    if src is not None and not _monopoly(src):
        fixed, _ = _repair_prior(s, src)
        widths = [max(MIN_COL_PERCENT, round(w * 100)) for w in fixed]
    else:
        weights = _content_weights(s)
        total = sum(weights) or 1.0
        widths = [max(MIN_COL_PERCENT, round(x / total * 100))
                  for x in weights]
    return enforce_photo_widths(_clamp_min(widths), s)


def _fallback_visual(ti: int, s: TSkeleton, tree: DocTree) -> TVisualTable:
    return TVisualTable(table_index=ti, col_widths=_base_widths(s, tree))


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
               pm: PromptManager, art_dir: Path, *,
               feedback: list[str] | None = None
               ) -> tuple[list[TVisualTable], list[str]]:
    """骨架 → 视觉参数（断点安全）。返回 (逐表视觉参数, W 级报告行)。

    feedback 模式（抽检意见重跑）：绕过幂等门，旧参数锚定最小修改；失败
    保留旧参数返回（调用方按收敛处理），不走确定性兜底——兜底会丢掉
    已验证可用的参数引发震荡。
    """
    art_path = art_dir / "tblvisual.json"
    if feedback is None and art_path.exists():
        got = _load_artifact(art_path, skeletons)
        if got is not None:
            return got

    old: list[TVisualTable] | None = None
    if feedback is not None:
        if art_path.exists():
            got = _load_artifact(art_path, skeletons)
            if got is not None:
                old = got[0]
        if old is None:
            return [], ["[tblvisual] W-VISUAL-FEEDBACK-FAIL 无旧参数可锚定，跳过意见重跑"]

    visuals: list[TVisualTable] = []
    report: list[str] = []
    bases = [_base_widths(s, tree) for s in skeletons]
    source = "llm-feedback" if feedback is not None else "llm-heights+base-widths"
    stats = [_table_stats(ti, s, bases[ti] if feedback is None else None)
             for ti, s in enumerate(skeletons)]
    system = pm.render("tblvisual/system.md")
    if feedback is not None:
        base_user = pm.render(
            "tblvisual/feedback.j2", tables=stats,
            content_width_cm=CONTENT_WIDTH_CM, feedback=feedback,
            old_params=json.dumps([v.model_dump() for v in old],
                                  ensure_ascii=False))
    else:
        base_user = pm.render("tblvisual/user.j2", tables=stats,
                              content_width_cm=CONTENT_WIDTH_CM)
    out: VisualOut | None = None
    errors: list[str] = []
    for attempt in (1, 2):
        if attempt == 1:
            user = base_user
        else:
            user = pm.render("tblvisual/retry.j2", errors=errors[:10],
                             context=base_user)
        try:
            out = client.structured(
                VisualOut,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                stage=f"tblvisual{'-fb' if feedback is not None else ''}#a{attempt}")
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
        if feedback is None:
            # F1: 首轮列宽一律确定性基准（源比例优先），LLM 输出的列宽弃用
            for ti, s in enumerate(skeletons):
                visuals.append(TVisualTable(
                    table_index=ti, col_widths=bases[ti],
                    row_heights=by_index[ti].row_heights))
                src = _src_widths(s, tree)
                if src is not None:
                    if _monopoly(src):
                        report.append(f"[tblvisual] t{ti}: 源表列宽异常"
                                      f"（单列垄断），改用内容当量基准")
                    else:
                        _, n = _repair_prior(s, src)
                        if n:
                            report.append(
                                f"[tblvisual] t{ti}: 源表列宽短标签反转"
                                f"修正 {n} 对（标签列宽搬回书写列）")
                        else:
                            report.append(f"[tblvisual] t{ti}: 列宽采用源表比例基准")
        else:
            # 意见重跑：LLM 锚定旧参数调整；照片最小宽钳制兜底（幂等）
            visuals = [
                TVisualTable(
                    table_index=ti,
                    col_widths=enforce_photo_widths(
                        renormalize_widths(by_index[ti].col_widths),
                        skeletons[ti]),
                    row_heights=by_index[ti].row_heights)
                for ti in range(len(skeletons))]
    elif feedback is not None:
        reason = errors[0][:60] if errors else "未知"
        return old, [f"[tblvisual] W-VISUAL-FEEDBACK-FAIL 意见重跑未成功：{reason}（保留原参数）"]
    else:
        source = "fallback"
        reason = errors[0][:60] if errors else "未知"
        for ti, s in enumerate(skeletons):
            visuals.append(_fallback_visual(ti, s, tree))
            report.append(f"[tblvisual] W-VISUAL-FALLBACK t{ti}: {reason}（确定性兜底列宽）")

    # 照片行高钳制（F2）：确定性后处理——首轮 / 意见重跑统一生效（断点
    # 续读的旧产物已含钳制结果，幂等最小值语义不重复加高）
    visuals = [
        TVisualTable(
            table_index=v.table_index, col_widths=v.col_widths,
            row_heights=(enforce_photo_heights(v.row_heights,
                                               skeletons[v.table_index])
                         if v.row_heights is not None else None))
        for v in visuals]

    art_dir.mkdir(parents=True, exist_ok=True)
    art_path.write_text(json.dumps(
        {"schema": "tblvisual/1.0", "source": source,
         "tables": [v.model_dump() for v in visuals], "report": report},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return visuals, report
