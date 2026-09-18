from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.pipeline.genre import LetterFrame, is_letter_frame_line
from app.schema.docir import (
    ClosingBlock,
    DocIssue,
    DocIR,
    DocIRMeta,
    DocTitleBlock,
    HeadingBlock,
    ParaBlock,
    SalutationBlock,
    SectionIR,
    SignatureBlock,
    TableBlock,
    validate_section_ir,
)
from app.schema.docplan import DocPlan, DocPlanItem
from app.schema.doctree import DocTree
from app.schema.enums import Genre, IssueCode

log = logging.getLogger(__name__)


class DocFillError(Exception):
    pass


class _ParasOut(BaseModel):
    """letter 正文 / 报告前言：只出段落。"""

    paras: list[str] = Field(default_factory=list)


class _SectionOut(BaseModel):
    """报告/表格文档章节：标题改写 + 段落。"""

    heading: str
    paras: list[str] = Field(default_factory=list)


def _section_map(tree: DocTree) -> dict[str, object]:
    out: dict[str, object] = {}
    for top in tree.sections:
        for s in top.walk():
            out[s.section_id] = s
    return out


def _has_prose(item: DocPlanItem, tree: DocTree) -> bool:
    """该节 src_refs 指向的节自身是否有非表格正文（子节是独立 item，不算）。"""
    secs = _section_map(tree)
    for ref in item.src_refs:
        sec = secs.get(ref)
        if sec is not None and any(
                b.kind != "table" and b.text.strip() for b in sec.blocks):
            return True
    return False


def _fill_deterministic(item: DocPlanItem) -> SectionIR:
    """源节无正文素材（纯表格节/空容器节）：标题照抄，零 LLM。"""
    blocks = []
    if item.heading_level >= 1 and item.heading.strip():
        blocks.append(HeadingBlock(level=min(item.heading_level, 2),
                                   text=item.heading))
    return SectionIR(section_id=item.section_id, blocks=blocks)


def fill_section(item: DocPlanItem, plan: DocPlan, tree: DocTree,
                 client: LLMClient, pm: PromptManager | None = None,
                 prior_errors: str = "") -> SectionIR:
    """单节规划 → SectionIR。无正文素材走确定性路径；否则 LLM 仿写 + 一次纠错重试。

    prior_errors：QA 定向重 fill 时传入该节上一版的违规明细，作为首轮纠错上下文。
    """
    if not _has_prose(item, tree):
        return _fill_deterministic(item)

    pm = pm or PromptManager()
    # letter 一律只出正文段；报告/表格文档的根前言（level 0）同样无标题
    wants_heading = plan.genre is not Genre.LETTER and item.heading_level >= 1
    schema = _SectionOut if wants_heading else _ParasOut
    mode = "section" if wants_heading else "paras"
    source_text = tree.resolve(item.src_refs)

    errors = prior_errors
    for _attempt in (1, 2):
        user = pm.render(
            "docfill/user.j2", seq=item.seq, genre=plan.genre.value, mode=mode,
            heading_draft=item.heading if wants_heading else "",
            source_text=source_text, errors=errors)
        messages = [
            {"role": "system", "content": pm.render("docfill/system.md")},
            {"role": "user", "content": user},
        ]
        out = client.structured(schema, messages, stage=f"docfill/{item.section_id}")

        if wants_heading:
            blocks: list = [HeadingBlock(
                level=min(max(item.heading_level, 1), 2),
                text=(out.heading.strip() or item.heading))]
        else:
            blocks = []
        blocks.extend(ParaBlock(text=p) for p in out.paras)
        sec = SectionIR(section_id=item.section_id, blocks=blocks)

        issues = [i for i in validate_section_ir(sec, plan.genre)
                  if i.rule is not IssueCode.W_DOC_PARA_LONG]
        if plan.genre is Genre.LETTER:
            for b in sec.blocks:
                if is_letter_frame_line(b.text):
                    issues.append(DocIssue(
                        rule=IssueCode.V_DOC_STRUCTURE,
                        detail=f"信件框架行不得出现在正文：{b.text}"))
                    break
        if not issues:
            return sec
        errors = "\n".join(f"- [{i.rule.value}] {i.detail}" for i in issues)

    raise DocFillError(f"节 {item.section_id} 两次填充均未通过校验：\n{errors}")


def fill_all_sections(plan: DocPlan, tree: DocTree, client: LLMClient,
                      sections_dir: Path, pm: PromptManager | None = None,
                      on_progress=None) -> dict[str, SectionIR]:
    """逐节并行 fill；每节即落盘（sec_XX.ir.json），重入跳过已完成节。"""
    pm = pm or PromptManager()
    sections_dir.mkdir(parents=True, exist_ok=True)
    seq_of = {it.section_id: it.seq for it in plan.items}

    result: dict[str, SectionIR] = {}
    todo: list[DocPlanItem] = []
    for item in plan.items:
        path = sections_dir / f"sec_{item.seq:02d}.ir.json"
        if path.exists():
            result[item.section_id] = SectionIR.model_validate_json(
                path.read_text(encoding="utf-8"))
        else:
            todo.append(item)
    total = len(plan.items)
    if on_progress and result:
        on_progress(len(result), total)

    if todo:
        workers = max(1, get_settings().fill_workers)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="docfill") as ex:
            futs = [ex.submit(fill_section, it, plan, tree, client, pm) for it in todo]
            for fut in as_completed(futs):
                sec = fut.result()  # 异常冒泡为 job 失败；已落盘节保留可续跑
                path = sections_dir / f"sec_{seq_of[sec.section_id]:02d}.ir.json"
                path.write_text(sec.model_dump_json(indent=2), encoding="utf-8")
                result[sec.section_id] = sec
                if on_progress:
                    on_progress(len(result), total)
    return result


def assemble_docir(plan: DocPlan, sections: dict[str, SectionIR], tree: DocTree,
                   frame: LetterFrame | None = None) -> DocIR:
    """SectionIR 集合 → DocIR。

    - doc_title / letter 框架块 / 表格全部确定性 verbatim，不经 LLM
    - heading 防跳级钳制（首个 level 2 提为 1）
    - 表格挂在该节块序列末尾
    """
    tables = {t.table_id: t for t in tree.tables}
    blocks: list = [DocTitleBlock(text=plan.title)]
    last_level = 0

    for item in sorted(plan.items, key=lambda x: x.seq):
        sec = sections.get(item.section_id)
        if sec is None:
            raise DocFillError(f"缺少节 fill 产物：{item.section_id}")
        for b in sec.blocks:
            if isinstance(b, HeadingBlock):
                level = max(1, min(b.level, last_level + 1))
                last_level = level
                blocks.append(HeadingBlock(level=level, text=b.text))
            else:
                blocks.append(b)
        for tid in item.table_ids:
            t = tables.get(tid)
            if t is None:
                raise DocFillError(f"规划引用了不存在的表格：{tid}")
            blocks.append(TableBlock(table_id=tid, header=list(t.header),
                                     rows=[list(r) for r in t.rows]))

    if plan.genre is Genre.LETTER and frame:
        if frame.salutation:
            blocks.insert(1, SalutationBlock(text=frame.salutation))
        if frame.closing:
            blocks.append(ClosingBlock(lines=list(frame.closing)))
        if frame.signer or frame.date:
            blocks.append(SignatureBlock(signer=frame.signer or "",
                                         date=frame.date or ""))

    return DocIR(meta=DocIRMeta(title=plan.title, genre=plan.genre), blocks=blocks)
