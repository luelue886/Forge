"""form+PDF 表格骨架 schema 与确定性校验器（T 轮新分支）。

表格架构师 LLM 的输出模型：物理行网格 + 格级 colspan/rowspan（HTML 语义，
跨行格只在起始行出现一次，后续行不重复）。不设 merge_rules —— 占位信息
已完整携带，双份真相只会打架。

校验器全部确定性：validate_skeleton 做占位网格游标走格（不重叠、不越界、
无空洞），validate_visual 做列宽/行高边界，coverage_missing 做源池文本
覆盖与占位符 id 完整性（掩码态与回填态骨架都适用）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schema.textlen import text_weight

MAX_GRID_CELLS = 2000  # 单表规模上限（行×列）
MIN_COL_PERCENT = 4
COL_SUM_RANGE = (98, 102)
ROW_HEIGHT_RANGE = (14, 400)

PLACEHOLDER_RE = re.compile(r"⟦(\d+)⟧")


class TCell(BaseModel):
    content: str = ""
    colspan: int = 1
    rowspan: int = 1
    style: Literal["header", "label", "input", "option", "note", "declare"] = "input"


class TRow(BaseModel):
    cells: list[TCell] = Field(default_factory=list)


class TSkeleton(BaseModel):
    table_title: str = ""
    total_cols: int = 1
    rows: list[TRow] = Field(default_factory=list)
    src_tables: list[str] = Field(default_factory=list)  # 吸收的源碎片 table_id


class ArchitectOut(BaseModel):
    tables: list[TSkeleton] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="before")
    @classmethod
    def _wrap_bare_table(cls, data):
        # 单逻辑表文档上 glm 常直接输出裸 TSkeleton（顶层 table_title/total_cols/
        # rows），而不是 {"tables":[…]} 包装——容忍之并包装
        if isinstance(data, dict) and "tables" not in data and "rows" in data:
            return {"tables": [data]}
        return data


class TVisualTable(BaseModel):
    table_index: int  # 骨架序号（0 基）
    col_widths: list[int] = Field(default_factory=list)  # 百分比整数
    row_heights: list[int] | None = None  # pt；None = 不指定


class VisualOut(BaseModel):
    tables: list[TVisualTable] = Field(default_factory=list)


@dataclass
class MaskPool:
    """全文档表格格文本掩码池：去空白去重的格文本 → 全局 ⟦N⟧ 编号。

    values：全局占位符 id → 原数字 token（全文档唯一，跨碎片重复文本映射
    同一条目、同一组占位符）。prose_masked：正文行掩码文本，只供架构师看
    语境，不参与覆盖校验。
    """

    texts: dict[str, str] = field(default_factory=dict)  # 去空白原文 → 掩码文本
    values: dict[int, str] = field(default_factory=dict)  # id → 原数字
    prose_masked: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)  # texts 插入序


def _despace(s: str) -> str:
    return re.sub(r"\s+", "", s)


def walk_grid(s: TSkeleton) -> tuple[dict[tuple[int, int], TCell],
                                     dict[tuple[int, int], TCell]]:
    """游标走格 → (锚点格, 全占位格)。锚点 = 跨行/列格的起始位。

    要求骨架已过 validate_skeleton（不重叠不越界无空洞）。
    """
    anchors: dict[tuple[int, int], TCell] = {}
    grid: dict[tuple[int, int], TCell] = {}
    for r, row in enumerate(s.rows):
        c = 0
        for cell in row.cells:
            while (r, c) in grid:
                c += 1
            anchors[(r, c)] = cell
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    grid[(r + dr, c + dc)] = cell
            c += cell.colspan
    return anchors, grid


def validate_skeleton(s: TSkeleton) -> list[str]:
    """占位网格游标走格：不重叠、不越界、无空洞。返回错误清单（空即合法）。"""
    errors: list[str] = []
    name = s.table_title or "未命名表"
    if s.total_cols < 1:
        return [f"{name}: total_cols 必须 ≥1"]
    if not s.rows:
        return [f"{name}: rows 为空"]
    if len(s.rows) * s.total_cols > MAX_GRID_CELLS:
        errors.append(f"{name}: 规模超限 {len(s.rows)}行×{s.total_cols}列"
                      f" > {MAX_GRID_CELLS}")
    grid: set[tuple[int, int]] = set()
    for r, row in enumerate(s.rows):
        if not row.cells:
            errors.append(f"{name}: 第{r}行没有任何格子")
            continue
        c = 0
        for cell in row.cells:
            while c < s.total_cols and (r, c) in grid:
                c += 1  # 跳过上方 rowspan 延伸占位
            if cell.colspan < 1 or cell.rowspan < 1:
                errors.append(f"{name}: 第{r}行存在 colspan/rowspan <1 的格")
                continue
            if c + cell.colspan > s.total_cols:
                errors.append(f"{name}: 第{r}行第{c}列起 colspan={cell.colspan}"
                              f" 越界（total_cols={s.total_cols}）")
                c += cell.colspan
                continue
            if r + cell.rowspan > len(s.rows):
                errors.append(f"{name}: 第{r}行第{c}列格 rowspan={cell.rowspan}"
                              f" 超出总行数 {len(s.rows)}")
            overlap = False
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    pos = (r + dr, c + dc)
                    if pos in grid:
                        if not overlap:
                            errors.append(f"{name}: 第{r}行第{c}列格"
                                          f"（span {cell.rowspan}×{cell.colspan}）"
                                          f"与已占位格重叠")
                            overlap = True
                    else:
                        grid.add(pos)
            c += cell.colspan
    holes = [(r, c) for r in range(len(s.rows))
             for c in range(s.total_cols) if (r, c) not in grid]
    if holes:
        r0, c0 = holes[0]
        errors.append(f"{name}: 网格有 {len(holes)} 处空洞（首处 第{r0}行第{c0}列）")
    return errors


def validate_visual(v: TVisualTable, n_cols: int, n_rows: int) -> list[str]:
    errors: list[str] = []
    if len(v.col_widths) != n_cols:
        errors.append(f"表{v.table_index}: col_widths 数量 {len(v.col_widths)}"
                      f" ≠ 列数 {n_cols}")
    total = sum(v.col_widths)
    lo, hi = COL_SUM_RANGE
    if not lo <= total <= hi:
        errors.append(f"表{v.table_index}: col_widths 总和 {total} 不在 [{lo},{hi}]")
    if any(w < MIN_COL_PERCENT for w in v.col_widths):
        errors.append(f"表{v.table_index}: 存在 <{MIN_COL_PERCENT}% 的列宽")
    if v.row_heights is not None:
        if len(v.row_heights) != n_rows:
            errors.append(f"表{v.table_index}: row_heights 数量 {len(v.row_heights)}"
                          f" ≠ 行数 {n_rows}")
        hlo, hhi = ROW_HEIGHT_RANGE
        bad = [h for h in v.row_heights if not hlo <= h <= hhi]
        if bad:
            errors.append(f"表{v.table_index}: 行高 {bad[:3]}pt 超出 [{hlo},{hhi}]pt")
    return errors


def renormalize_widths(widths: list[int]) -> list[int]:
    """正整数列宽 → 和恰为 100（最大余数法）。校验通过后调用。"""
    total = sum(widths) or 1
    scaled = [w * 100 / total for w in widths]
    floors = [int(x) for x in scaled]
    remain = 100 - sum(floors)
    order = sorted(range(len(widths)),
                   key=lambda i: scaled[i] - floors[i], reverse=True)
    for i in range(remain):
        floors[order[i % len(order)]] += 1
    return floors


def coverage_missing(skeletons: list[TSkeleton], pool: MaskPool) -> list[str]:
    """源池文本（权重 ≥2 当量）须在骨架拼接内容中出现；骨架占位符 id 须存在于池。

    掩码态与回填态骨架都适用：掩码文本与去空白原文任一命中即算覆盖；
    表题（table_title 会渲染为表上方题注）同样计入覆盖。
    解析器把同一逻辑文本拆成多个碎片格（竖排侧栏/跨片重复）——缺失文本若是
    某个已覆盖更长池条目的子串，内容已无损失，豁免。
    错误信息用掩码文本——数字永不以明文回到 LLM。
    """
    joined = _despace("".join(
        [s.table_title for s in skeletons]
        + [cell.content for s in skeletons
           for row in s.rows for cell in row.cells]))
    covered = {orig for orig, masked in pool.texts.items()
               if _despace(masked) in joined or _despace(orig) in joined}
    errors: list[str] = []
    for orig, masked in pool.texts.items():
        if text_weight(orig) < 2 or orig in covered:
            continue  # 单字符/% 等噪声豁免
        if any(orig in c and len(c) > len(orig) for c in covered):
            continue  # 碎片子串被更长已覆盖条目吸收
        errors.append(f"源池文本未进骨架：{_despace(masked)[:24]}")
    known = set(pool.values)
    for m in PLACEHOLDER_RE.finditer(joined):
        if int(m.group(1)) not in known:
            errors.append(f"未知占位符 ⟦{m.group(1)}⟧（不在源池）")
    return errors
