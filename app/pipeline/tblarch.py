"""表格架构师：PDF 表格碎片 → 逻辑表 JSON 骨架（form+PDF 分支第 1 步）。

解析器把逻辑表拆成碎片（竖排侧栏单字成表/跨页断裂/行内换行拆行），确定
性几何恢复无法跨碎片重组——本模块把掩码碎片交给 LLM 还原逻辑结构（只还
原、不创作），再用确定性回填杜绝回声腐蚀：

1. build_mask_pool：全文档表格格文本去空白去重 → 全局 ⟦N⟧ 数字掩码池；
   数字永不以明文进 prompt（错误清单回传 LLM 时同样用掩码文本）。
2. run_architect：掩码碎片视图 + 三种碎片模式指导 → ArchitectOut →
   validate_skeleton + coverage_missing → 1 次定向重试（错误清单附入）→
   仍败 raise FormBranchFallback（编排层回落旧链路）。
3. 确定性回填：骨架格内容 := 命中的池原文（数字占位符回填池值），池外
   残留保留架构师回声并落 W 报告行。

断点：artifacts/tblarch.json 存在且过全部校验 → 跳过 LLM。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.qa.numbers import number_spans
from app.schema.doctree import DocTree
from app.schema.docplan import DocPlan
from app.schema.tblskeleton import (
    PLACEHOLDER_RE,
    ArchitectOut,
    MaskPool,
    TSkeleton,
    coverage_missing,
    validate_skeleton,
)

log = logging.getLogger(__name__)

# 池外残留判"有意义字符"：字母/数字/CJK 统一表意文字（标点豁免——架构师
# 拼接碎片时常补 ：/（）等连接符）
_MEANING = re.compile(r"[0-9A-Za-zＡ-Ｚａ-ｚ０-９一-鿿]")
_MAX_RETRY_ERRORS = 20


class FormBranchFallback(Exception):
    """表格架构师重建失败 → 整文档回落既有链路（fill/QA/tablefill/render）。"""


def _despace(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _mask_into(text: str, pool: MaskPool) -> str:
    """文本数字 → 全局递增 ⟦N⟧（值入池），返回掩码文本。"""
    parts: list[str] = []
    last = 0
    for s, e in number_spans(text):
        parts.append(text[last:s])
        nid = len(pool.values) + 1
        pool.values[nid] = text[s:e]
        parts.append(f"⟦{nid}⟧")
        last = e
    parts.append(text[last:])
    return "".join(parts)


def build_mask_pool(tree: DocTree) -> MaskPool:
    """全部表格格文本（表题+表头+数据行）→ 去空白去重的全局掩码池。

    跨碎片重复文本映射同一池条目；正文行掩码后只入 prose_masked 供架构师
    看语境，不参与覆盖校验。
    """
    pool = MaskPool()
    for t in tree.tables:
        texts = ([t.caption] if t.caption else []) + list(t.header or [])
        texts += [c for row in t.rows for c in row]
        for text in texts:
            key = _despace(text)
            if not key or key in pool.texts:
                continue
            pool.texts[key] = _mask_into(key, pool)
            pool.order.append(key)
    for sec in tree.sections:
        for s in sec.walk():
            for b in s.blocks:
                if b.kind in ("para", "list_item") and b.text.strip():
                    pool.prose_masked.append(_mask_into(b.text, pool))
    return pool


def _masked_cell(cell: str, pool: MaskPool) -> str:
    key = _despace(cell)
    if not key:
        return ""
    if key not in pool.texts:  # 防御：build_mask_pool 全量入池，理论不可达
        raise AssertionError(f"掩码池缺失格文本：{key[:20]}")
    return pool.texts[key]


def _fragments(tree: DocTree, pool: MaskPool) -> list[str]:
    """逐碎片紧凑 JSON 视图（格文本全部掩码态）。"""
    lines: list[str] = []
    for t in tree.tables:
        grid = ([t.header] if t.header else []) + list(t.rows)
        obj: dict = {"id": t.table_id}
        if t.caption:
            obj["cap"] = _masked_cell(t.caption, pool)
        obj["grid"] = [[_masked_cell(c, pool) for c in row] for row in grid]
        if t.col_widths:
            obj["cw"] = [round(w * 100) for w in t.col_widths]
        if t.merges:
            obj["mg"] = t.merges  # 合并区提示 [行,列,行跨,列跨]，网格坐标
        lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    return lines


def _check(out: ArchitectOut, pool: MaskPool) -> list[str]:
    if not out.tables:
        return ["未返回任何表骨架"]
    errors: list[str] = []
    for s in out.tables:
        errors += validate_skeleton(s)
    errors += coverage_missing(out.tables, pool)
    return errors


def _rebuild_cell(masked: str, pool: MaskPool) -> tuple[str, bool]:
    """骨架格掩码文本 → (回填文本, 是否完全由源池解释)。

    命中池条目的片段替换回原文（去空白态匹配，长匹配优先）；残留 ⟦N⟧
    回填池值（id 未知则原样保留，由上层 Fallback 把关）。池外残留文本
    保留架构师回声，由调用方落 W 报告行。
    """
    s = _despace(masked)
    matches: list[tuple[int, int, str]] = []
    for orig, mtext in pool.texts.items():
        if not mtext:
            continue
        idx = 0
        while True:
            i = s.find(mtext, idx)
            if i < 0:
                break
            matches.append((i, i + len(mtext), orig))
            idx = i + len(mtext)
    matches.sort(key=lambda m: -(m[1] - m[0]))  # 长匹配优先
    chosen: list[tuple[int, int, str]] = []
    for st, en, orig in matches:
        if any(st < e2 and s2 < en for s2, e2, _ in chosen):
            continue
        chosen.append((st, en, orig))
    chosen.sort()
    parts: list[str] = []
    leftover = ""
    pos = 0
    for st, en, orig in chosen:
        parts.append(s[pos:st])
        leftover += s[pos:st]
        parts.append(orig)
        pos = en
    tail = s[pos:]
    parts.append(tail)
    leftover += tail
    result = PLACEHOLDER_RE.sub(
        lambda m: pool.values.get(int(m.group(1)), m.group(0)), "".join(parts))
    explained = not _MEANING.search(PLACEHOLDER_RE.sub("", leftover))
    return result, explained


def _rebuild_skeleton(s: TSkeleton, pool: MaskPool, ti: int,
                      report: list[str]) -> None:
    if s.table_title:
        s.table_title = PLACEHOLDER_RE.sub(
            lambda m: pool.values.get(int(m.group(1)), m.group(0)), s.table_title)
    for r, row in enumerate(s.rows):
        for c, cell in enumerate(row.cells):
            if not cell.content.strip():
                continue
            new_text, explained = _rebuild_cell(cell.content, pool)
            if not explained:
                report.append(f"[tblarch] W-UNMATCHED t{ti} r{r}c{c}: "
                              f"骨架格含源池外文本，保留架构师回声")
            cell.content = new_text


def _load_artifact(path: Path, pool: MaskPool
                   ) -> tuple[list[TSkeleton], list[str]] | None:
    """断点产物逐项复检（结构 + 覆盖）；任何失效 → 整体重做。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tables = [TSkeleton.model_validate(t) for t in data["tables"]]
        report = [str(x) for x in data.get("report", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    errors = [e for s in tables for e in validate_skeleton(s)]
    errors += coverage_missing(tables, pool)
    if errors:
        log.info("tblarch 断点产物校验失败，重做：%s", errors[:3])
        return None
    return tables, report


def run_architect(tree: DocTree, plan: DocPlan, client: LLMClient,
                  pm: PromptManager, art_dir: Path
                  ) -> tuple[list[TSkeleton], MaskPool, list[str]]:
    """碎片 → 逻辑骨架（断点安全）。返回 (骨架列表, 掩码池, W 级报告行)。

    两轮校验不过 / 回填后残留占位符 → FormBranchFallback。
    """
    pool = build_mask_pool(tree)
    if not pool.order:
        raise FormBranchFallback("源表格无任何格文本，无重建对象")

    art_path = art_dir / "tblarch.json"
    if art_path.exists():
        got = _load_artifact(art_path, pool)
        if got is not None:
            return got[0], pool, got[1]

    title_masked = _mask_into(plan.title, pool)
    user_ctx = pm.render("tblarch/user.j2", title=title_masked,
                         prose=pool.prose_masked,
                         fragments=_fragments(tree, pool))
    system = pm.render("tblarch/system.md")
    errors: list[str] = []
    out: ArchitectOut | None = None
    for attempt in (1, 2):
        if attempt == 1:
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_ctx},
            ]
        else:
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": pm.render(
                    "tblarch/retry.j2", errors=errors[:_MAX_RETRY_ERRORS],
                    context=user_ctx)},
            ]
        try:
            out = client.structured(ArchitectOut, messages,
                                    stage=f"tblarch#a{attempt}")
        except Exception as e:  # noqa: BLE001 — LLM 连续不可用 → 回落旧链路
            raise FormBranchFallback(f"架构师 LLM 调用失败：{e}") from e
        errors = _check(out, pool)
        if not errors:
            break
    if errors or out is None:
        raise FormBranchFallback(
            f"架构师两轮未过校验（{len(errors)} 项）："
            + "；".join(errors[:5]))

    report: list[str] = []
    for ti, s in enumerate(out.tables):
        _rebuild_skeleton(s, pool, ti, report)
    leftover = [s.table_title or f"t{ti}" for ti, s in enumerate(out.tables)
                if "⟦" in s.table_title
                or any("⟦" in c.content or "⟧" in c.content
                       for row in s.rows for c in row.cells)]
    if leftover:
        raise FormBranchFallback(f"回填后仍残留占位符：{leftover}")

    art_dir.mkdir(parents=True, exist_ok=True)
    art_path.write_text(json.dumps(
        {"schema": "tblarch/1.0",
         "tables": [s.model_dump() for s in out.tables],
         "report": report}, ensure_ascii=False, indent=2), encoding="utf-8")
    return out.tables, pool, report
