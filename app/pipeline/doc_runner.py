"""文档线流水线：parse → docplan → ★PLANNED → 逐节 fill → DocIR → QA 修复
→ docx 渲染 → Word COM PDF → PyMuPDF 预览 → DONE。每阶段先查产物，幂等可续跑。"""

from __future__ import annotations

import logging
from pathlib import Path

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.pipeline.docfill import assemble_docir, fill_all_sections
from app.pipeline.docplan import build_doc_plan
from app.pipeline.genre import extract_letter_frame
from app.pipeline.runner import _JobLike, _artifacts, _load_json, _save_json
from app.pipeline.tablefill import fill_all_tables
from app.qa.docqa import qa_and_repair
from app.render.docx_render import render_docir_to_docx
from app.render.pdf_preview import export_pdf_page_pngs
from app.schema.docir import DocIR, validate_docir
from app.schema.docplan import DocPlan
from app.schema.doctree import DocTree
from app.schema.enums import Genre, IssueCode, JobStatus

log = logging.getLogger(__name__)


def _render_source(job: _JobLike) -> Path | None:
    """渲染期源 docx（表格/图片 XML 搬运的锚点）：.docx 原样，.doc 用转换产物。"""
    src = job.dir / "upload" / job.source_name
    if src.suffix.lower() == ".doc":
        src = src.with_name(f"{Path(job.source_name).stem}.converted.docx")
    return src if src.exists() else None


def run_doc_pipeline(job: _JobLike, client: LLMClient | None = None,
                     com_export: bool = True) -> None:
    client = client or LLMClient(log_path=job.dir / "logs" / "llm_calls.jsonl")
    pm = PromptManager()
    art = _artifacts(job)

    # ---- docplan（确定性规划 + 体裁识别，文档线唯一 pre-fill LLM 点）----
    docplan_path = art / "docplan.json"
    if not docplan_path.exists():
        tree = _load_json(art / "doctree.json", DocTree)
        job.set_status(JobStatus.UNDERSTOOD, "体裁识别 + 规划")
        job.save_state()
        _save_json(docplan_path, build_doc_plan(tree, client, pm))
        job.save_state()

    plan = _load_json(docplan_path, DocPlan)
    if not (job.confirmed or getattr(job, "auto_confirm", False)):
        job.set_status(JobStatus.PLANNED,
                       f"{len(plan.items)} 节 · 体裁 {plan.genre.value} 待确认")
        job.save_state()
        return

    # ---- fill（逐节落盘，可断点续跑）----
    tree = _load_json(art / "doctree.json", DocTree)
    total = len(plan.items)
    job.set_status(JobStatus.GENERATING, f"fill 0/{total}")
    job.save_state()

    def _progress(done: int, _total: int) -> None:
        job.set_status(JobStatus.GENERATING, f"fill {done}/{_total}")
        job.save_state()

    sections = fill_all_sections(plan, tree, client, job.dir / "sections", pm,
                                 on_progress=_progress)

    # ---- QA：数字溯源 + 10-gram，blocking 定向重 fill ≤2 轮 ----
    job.set_status(JobStatus.QA, "数字溯源 + 防抄袭检查")
    job.save_state()
    sections, qa_issues = qa_and_repair(plan, tree, sections, client, pm,
                                        job.dir / "sections")

    # ---- 表格内容仿写（长文本格 ⟦N⟧ 掩码，退格照搬兜底，断点落 artifacts/tables）----
    table_rewrites, tf_report = fill_all_tables(
        tree, client, pm, job.dir / "artifacts" / "tables")

    # ---- 组装 DocIR + 校验 ----
    frame = extract_letter_frame(tree) if plan.genre is Genre.LETTER else None
    doc = assemble_docir(plan, sections, tree, frame, table_rewrites=table_rewrites)
    blocking = [i for i in validate_docir(doc)
                if i.rule is not IssueCode.W_DOC_PARA_LONG]
    if blocking:
        raise RuntimeError("DocIR 未通过校验：\n" + "\n".join(
            f"[{i.rule.value}] {i.detail}" for i in blocking))
    _save_json(art / "docir.json", doc)

    lines = [f"[docqa] {i.section_id} {i.code.value}: {i.detail}"
             for i in qa_issues]
    lines.extend(tf_report)
    (art / "qa_report.txt").write_text(
        "\n".join(lines) or "（无问题）", encoding="utf-8")

    # ---- 渲染 docx → Word COM 转 PDF → 逐页预览 ----
    job.set_status(JobStatus.RENDERED, "render docx")
    job.save_state()
    source = _render_source(job)
    if source is None:
        log.warning("渲染期源文件缺失，表格/图片按规范样式重建（排版保真降级）")
    docx = render_docir_to_docx(doc, art / "output.docx", source=source)

    n_pages = 1
    if com_export:
        from app.services.com_export import export_docx_pdf

        job.set_status(JobStatus.QA, "导出 PDF")
        job.save_state()
        pdf = export_docx_pdf(docx, art / "output.pdf")
        import pymupdf

        with pymupdf.open(str(pdf)) as d:
            n_pages = len(d)
        export_pdf_page_pngs(pdf, job.dir / "pages")

    detail = f"{len(plan.items)} 节 · {n_pages} 页"
    if qa_issues:
        detail += f" · QA 残留 {len(qa_issues)} 项（见 qa_report.txt）"
    job.set_status(JobStatus.DONE, detail)
    job.save_state()
