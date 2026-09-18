"""文档线 QA：逐节数字溯源 + 10-gram 防抄袭，blocking 问题片段级微修复 ≤2 轮。

表格单元格 / letter 框架 / 文档标题是设计上的 verbatim，不进检查；
只查 fill 产出的仿写文本（heading + para），且逐块检查——块间拼接会
把"段尾句号+下个标题"凑成伪 10-gram，渲染产物里块与块本是分行呈现。

修复是片段级微重写：只把违规块发给 LLM 在现有文本上最小修订，改写后
复检通过才落位。不做整节从源文重新生成——那样每轮都是一次新的骰子，
实测会修好 A 片段又复现 B 片段（震荡），已通过的块绝不再送 LLM。
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.pipeline.genre import is_letter_frame_line
from app.qa.ngram import ngram_hits
from app.qa.numbers import check_numbers
from app.schema.docir import (
    DOC_LIMITS,
    HeadingBlock,
    ParaBlock,
    SectionIR,
    text_weight,
)
from app.schema.docplan import DocPlan
from app.schema.doctree import DocTree
from app.schema.enums import Genre, IssueCode, Severity

log = logging.getLogger(__name__)


class DocQAIssue(BaseModel):
    code: IssueCode
    severity: Severity
    section_id: str
    detail: str


def _text_issues(text: str, full_text: str) -> list[tuple[IssueCode, str]]:
    out: list[tuple[IssueCode, str]] = []
    for tok, ctx in check_numbers(text, full_text):
        out.append((IssueCode.E_NUM_UNTRACED, f"数字 {tok} 未在源文档出现：{ctx}"))
    for g in ngram_hits(text, full_text):
        out.append((IssueCode.E_PLAGIARISM, f"与源文连续 {len(g)} 字雷同：{g}"))
    return out


def check_sections(plan: DocPlan, tree: DocTree,
                   sections: dict[str, SectionIR]) -> list[DocQAIssue]:
    issues: list[DocQAIssue] = []
    full_text = tree.full_text
    for item in plan.items:
        sec = sections.get(item.section_id)
        if sec is None:
            continue
        for b in sec.blocks:
            if b.kind not in ("heading", "para") or not b.text.strip():
                continue
            for code, detail in _text_issues(b.text, full_text):
                issues.append(DocQAIssue(
                    code=code, severity=Severity.BLOCKING,
                    section_id=item.section_id,
                    detail=f"{detail}（{b.kind}）"))
    return issues


class _RepairOut(BaseModel):
    """片段级微修复：块编号 → 修订后整块文本。"""

    blocks: dict[str, str] = Field(default_factory=dict)


_REPAIR_HINTS = {
    IssueCode.E_PLAGIARISM:
        "打散该片段：调换语序、重组前后连接措辞、插入修饰语；"
        "≥10 字的专有名词短语须拆开或插入修饰语使用，不得整段照抄",
    IssueCode.E_NUM_UNTRACED:
        "删除该数字或改为不含具体数值的表述，严禁换成另一个编造数字",
}


def _block_ok(b: HeadingBlock | ParaBlock, genre: Genre,
              full_text: str) -> bool:
    if not b.text.strip() or _text_issues(b.text, full_text):
        return False
    if genre is Genre.LETTER and (isinstance(b, HeadingBlock)
                                   or is_letter_frame_line(b.text)):
        return False
    if isinstance(b, HeadingBlock) and \
            text_weight(b.text) > DOC_LIMITS["heading_weight"]:
        return False
    return True


def _block_index(key: str) -> int | None:
    """LLM 会把键写成 "3" 或模板式 "块 3"，两种都收。"""
    s = key.strip()
    if s.startswith("块"):
        s = s[1:].strip()
    try:
        return int(s)
    except ValueError:
        return None


def _repair_section(sec: SectionIR, plan: DocPlan, client: LLMClient,
                    pm: PromptManager, full_text: str) -> bool:
    """违规块送 LLM 微修订；改写后复检通过才替换原块。返回是否有改动。"""
    flagged: dict[int, list[tuple[IssueCode, str]]] = {}
    for idx, b in enumerate(sec.blocks):
        if b.kind not in ("heading", "para") or not b.text.strip():
            continue
        probs = _text_issues(b.text, full_text)
        if probs:
            flagged[idx] = probs
    if not flagged:
        return False

    lines: list[str] = []
    for idx in sorted(flagged):
        for code, detail in flagged[idx]:
            line = f"- [块 {idx}] [{code.value}] {detail}"
            hint = _REPAIR_HINTS.get(code)
            if hint:
                line += f"。修正方法：{hint}"
            lines.append(line)

    user = pm.render(
        "docrepair/user.j2",
        blocks=[{"idx": idx, "text": sec.blocks[idx].text}
                for idx in sorted(flagged)],
        errors="\n".join(lines))
    messages = [
        {"role": "system", "content": pm.render("docrepair/system.md")},
        {"role": "user", "content": user},
    ]
    try:
        out = client.structured(_RepairOut, messages,
                                stage=f"docrepair/{sec.section_id}")
    except Exception as e:  # noqa: BLE001 — 修复失败保留旧版
        log.warning("节 %s 微修复调用失败，保留旧版：%s", sec.section_id, e)
        return False

    changed = False
    for key, new_text in out.blocks.items():
        idx = _block_index(key)
        if idx is None or idx not in flagged:
            continue  # 非法/越权块一律不采纳（防 LLM 改写干净块）
        old = sec.blocks[idx]
        cand = (HeadingBlock(level=old.level, text=new_text.strip())
                if isinstance(old, HeadingBlock)
                else ParaBlock(text=new_text.strip()))
        if _block_ok(cand, plan.genre, full_text):
            sec.blocks[idx] = cand
            changed = True
    return changed


def qa_and_repair(plan: DocPlan, tree: DocTree,
                  sections: dict[str, SectionIR], client: LLMClient,
                  pm: PromptManager | None = None,
                  sections_dir: Path | None = None,
                  max_rounds: int = 2) -> tuple[dict[str, SectionIR], list[DocQAIssue]]:
    """跑 QA；blocking 问题的节做片段级微修复，≤max_rounds 轮。

    每轮只把仍违规的块送修；改写后复检通过的块落位，不过的保留旧版——
    问题数单调不增，已通过的块绝不再送 LLM。修复调用失败不致命，照常上报。
    """
    pm = pm or PromptManager()
    seq_of = {it.section_id: it.seq for it in plan.items}
    issues = check_sections(plan, tree, sections)
    for _round in range(1, max_rounds + 1):
        if not issues:
            break
        for sid in sorted({i.section_id for i in issues}):
            sec = sections.get(sid)
            if sec is None:
                continue
            _repair_section(sec, plan, client, pm, tree.full_text)
            if sections_dir is not None and sid in seq_of:
                p = sections_dir / f"sec_{seq_of[sid]:02d}.ir.json"
                p.write_text(sec.model_dump_json(indent=2), encoding="utf-8")
        issues = check_sections(plan, tree, sections)
    return sections, issues
