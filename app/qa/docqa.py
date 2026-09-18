"""文档线 QA：逐节数字溯源 + 10-gram 防抄袭，blocking 定向重 fill ≤2 轮。

表格单元格 / letter 框架 / 文档标题是设计上的 verbatim，不进检查；
只查 fill 产出的仿写文本（heading + para），且逐块检查——块间拼接会
把"段尾句号+下个标题"凑成伪 10-gram，渲染产物里块与块本是分行呈现。
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.pipeline.docfill import fill_section
from app.qa.ngram import ngram_hits
from app.qa.numbers import check_numbers
from app.schema.docplan import DocPlan
from app.schema.docir import SectionIR
from app.schema.doctree import DocTree
from app.schema.enums import IssueCode, Severity

log = logging.getLogger(__name__)


class DocQAIssue(BaseModel):
    code: IssueCode
    severity: Severity
    section_id: str
    detail: str


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
            for tok, ctx in check_numbers(b.text, full_text):
                issues.append(DocQAIssue(
                    code=IssueCode.E_NUM_UNTRACED, severity=Severity.BLOCKING,
                    section_id=item.section_id,
                    detail=f"数字 {tok} 未在源文档出现（{b.kind}）：{ctx}"))
            for g in ngram_hits(b.text, full_text):
                issues.append(DocQAIssue(
                    code=IssueCode.E_PLAGIARISM, severity=Severity.BLOCKING,
                    section_id=item.section_id,
                    detail=f"与源文连续 {len(g)} 字雷同（{b.kind}）：{g}"))
    return issues


_PLAGIARISM_HINT = (
    "。修正方法：保留其中数字与专名原样，数字前后的措辞都要重组，"
    "严禁保留“引导语+数字+后续短语”的原句式（如“实现营业收入X万元”改为"
    "“营业收入达到X万元”；“投入X万元，渠道建设”改为“X万元投向市场推广，"
    "另安排渠道建设”——调换语序打破雷同片段）")


def qa_and_repair(plan: DocPlan, tree: DocTree,
                  sections: dict[str, SectionIR], client: LLMClient,
                  pm: PromptManager | None = None,
                  sections_dir: Path | None = None,
                  max_rounds: int = 2) -> tuple[dict[str, SectionIR], list[DocQAIssue]]:
    """跑 QA；blocking 问题的节带着违规明细定向重 fill，≤max_rounds 轮。

    重 fill 抛错不致命：保留旧版本继续，残余问题照常上报。
    """
    pm = pm or PromptManager()
    issues = check_sections(plan, tree, sections)
    for _round in range(1, max_rounds + 1):
        if not issues:
            break
        hints: dict[str, list[str]] = {}
        for i in issues:
            line = f"- [{i.code.value}] {i.detail}"
            if i.code is IssueCode.E_PLAGIARISM:
                line += _PLAGIARISM_HINT
            hints.setdefault(i.section_id, []).append(line)
        for item in plan.items:
            if item.section_id not in hints:
                continue
            try:
                sec = fill_section(item, plan, tree, client, pm,
                                   prior_errors="\n".join(hints[item.section_id]))
            except Exception as e:  # noqa: BLE001 — 重 fill 失败保留旧版
                log.warning("节 %s 定向重 fill 失败，保留旧版：%s",
                            item.section_id, e)
                continue
            sections[item.section_id] = sec
            if sections_dir is not None:
                p = sections_dir / f"sec_{item.seq:02d}.ir.json"
                p.write_text(sec.model_dump_json(indent=2), encoding="utf-8")
        issues = check_sections(plan, tree, sections)
    return sections, issues
