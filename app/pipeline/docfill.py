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
    ImageBlock,
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
from app.schema.textlen import text_weight

log = logging.getLogger(__name__)


class DocFillError(Exception):
    pass


_FORM_PROSE_MIN = 30.0  # form 节源正文低于此汉字当量视为空模板，无素材可仿写


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


def _prose_weight(item: DocPlanItem, tree: DocTree) -> float:
    """src_refs 各节 para/list_item 文本当量合计（表格 flat_text 不计）。"""
    secs = _section_map(tree)
    total = 0.0
    for ref in item.src_refs:
        sec = secs.get(ref)
        if sec is not None:
            total += sum(text_weight(b.text) for b in sec.blocks
                         if b.kind in ("para", "list_item"))
    return total


def _fill_deterministic(item: DocPlanItem, genre: Genre) -> SectionIR:
    """源节无正文素材（纯表格节/空容器节/form 空模板）：标题照抄，零 LLM。

    仍过块级校验（豁免零正文）：源标题超长/体裁不允许 heading 时丢标题保零段
    ——这类块留着会被 doc 级校验拦下整个任务。
    """
    blocks = []
    if item.heading_level >= 1 and item.heading.strip():
        blocks.append(HeadingBlock(level=min(item.heading_level, 2),
                                   text=item.heading))
    sec = SectionIR(section_id=item.section_id, blocks=blocks)
    issues = validate_section_ir(sec, genre, allow_empty_prose=True)
    if issues:
        log.warning("节 %s 确定性零段丢弃标题：%s", item.section_id,
                    "；".join(i.detail for i in issues))
        sec.blocks = []
    return sec


def fill_section(item: DocPlanItem, plan: DocPlan, tree: DocTree,
                 client: LLMClient, pm: PromptManager | None = None) -> SectionIR:
    """单节规划 → SectionIR。无正文素材走确定性路径；否则 LLM 仿写 + 一次纠错重试。"""
    if not _has_prose(item, tree):
        return _fill_deterministic(item, plan.genre)
    if plan.genre is Genre.FORM and _prose_weight(item, tree) < _FORM_PROSE_MIN:
        # form 空模板合集（源正文 < 30 当量）：LLM 无素材只会编造营销文案
        return _fill_deterministic(item, plan.genre)

    pm = pm or PromptManager()
    # letter 一律只出正文段；报告/表格文档的根前言（level 0）同样无标题
    wants_heading = plan.genre is not Genre.LETTER and item.heading_level >= 1
    schema = _SectionOut if wants_heading else _ParasOut
    mode = "section" if wants_heading else "paras"
    source_text = tree.resolve(item.src_refs)

    errors = ""
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
                   frame: LetterFrame | None = None,
                   table_rewrites: dict[str, dict[str, str]] | None = None) -> DocIR:
    """SectionIR 集合 → DocIR。

    - doc_title / letter 框架块 / 表格全部确定性 verbatim，不经 LLM；
      表格长文本格的改写由 tablefill 产出，按 "r,c" 直接替换进 rows
      （rows 即最终内容，溯源由 artifacts/tables/*.json 承担）
    - heading 防跳级钳制（首个 level 2 提为 1）
    - 表格挂在该节块序列末尾
    """
    tables = {t.table_id: t for t in tree.tables}
    images = {im.image_id: im for im in tree.images}
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
            rows = [list(r) for r in t.rows]
            for key, text in (table_rewrites or {}).get(tid, {}).items():
                r_s, _, c_s = key.partition(",")
                if r_s.isdigit() and c_s.isdigit():
                    r, c = int(r_s), int(c_s)
                    if r < len(rows) and c < len(rows[r]):
                        rows[r][c] = text
            blocks.append(TableBlock(table_id=tid, header=list(t.header),
                                     rows=rows,
                                     src_index=t.src_index,
                                     col_widths=list(t.col_widths) if t.col_widths else None,
                                     merges=[list(m) for m in t.merges] if t.merges else None,
                                     row_heights=list(t.row_heights) if t.row_heights else None))
        for iid in item.image_ids:
            im = images.get(iid)
            if im is None:
                raise DocFillError(f"规划引用了不存在的图片：{iid}")
            blocks.append(ImageBlock(image_id=iid, body_index=im.body_index,
                                     page=im.page,
                                     bbox=list(im.bbox) if im.bbox else None,
                                     cx_emu=im.cx_emu, cy_emu=im.cy_emu))

    if plan.genre is Genre.LETTER and frame:
        if frame.salutation:
            blocks.insert(1, SalutationBlock(text=frame.salutation))
        if frame.closing:
            blocks.append(ClosingBlock(lines=list(frame.closing)))
        if frame.signer or frame.date:
            blocks.append(SignatureBlock(signer=frame.signer or "",
                                         date=frame.date or ""))

    return DocIR(meta=DocIRMeta(title=plan.title, genre=plan.genre), blocks=blocks)
