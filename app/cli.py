from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.schema.enums import Severity
from app.schema.slideir import Deck, validate_deck


def _cmd_render(args) -> int:
    if args.docir:
        return _cmd_render_docir(args)
    from app.render.capacity import capacity_issues
    from app.render.renderer import render_to_file
    from app.services.com_export import checklist, export_pngs

    deck = Deck.model_validate_json(Path(args.deck).read_text(encoding="utf-8"))

    blocking = 0

    schema_issues = validate_deck(deck)
    for i in schema_issues:
        print(f"[schema] p{i.page_no} {i.rule.value}: {i.detail}")
    if schema_issues:
        blocking += sum(1 for i in schema_issues)
        if not args.force:
            print("schema 校验未通过（--force 可跳过查看渲染效果）")
            return 1

    for q in capacity_issues(deck):
        tag = "blocking" if q.severity is Severity.BLOCKING else "cosmetic"
        print(f"[capacity/{tag}] p{q.page_no} {q.code.value}: {q.detail}")
        if q.severity is Severity.BLOCKING:
            blocking += 1

    out = Path(args.out)
    render_to_file(deck, args.skin, out)
    print(f"已渲染 {len(deck.slides)} 页 → {out}（皮肤 {args.skin}）")

    if args.pngs:
        pngs = export_pngs(out, Path(args.pngs))
        print(f"已导出 {len(pngs)} 张 PNG → {args.pngs}")
        for q in checklist(deck, out):
            print(f"[checklist/blocking] p{q.page_no} {q.code.value}: {q.detail}")
            blocking += 1

    print(f"完成：blocking={blocking}")
    return 1 if blocking else 0


def _cmd_render_docir(args) -> int:
    from app.render.docx_render import render_docir_to_docx
    from app.schema.docir import DocIR, validate_docir
    from app.schema.enums import IssueCode

    doc = DocIR.model_validate_json(Path(args.deck).read_text(encoding="utf-8"))
    issues = validate_docir(doc)
    blocking = [i for i in issues if i.rule is not IssueCode.W_DOC_PARA_LONG]
    for i in issues:
        tag = "blocking" if i in blocking else "warn"
        print(f"[schema/{tag}] {i.rule.value}: {i.detail}")
    if blocking and not args.force:
        print("schema 校验未通过（--force 可跳过查看渲染效果）")
        return 1

    out = render_docir_to_docx(doc, Path(args.out))
    print(f"已渲染 {len(doc.blocks)} 块 → {out}")
    if args.pdf:
        from app.services.com_export import export_docx_pdf

        pdf = export_docx_pdf(out, out.with_suffix(".pdf"))
        print(f"已导出 PDF → {pdf}")
    print(f"完成：blocking={len(blocking)}")
    return 1 if blocking else 0


def _cmd_export(args) -> int:
    from app.services.com_export import export_pngs

    pngs = export_pngs(Path(args.pptx), Path(args.out_dir))
    print(f"已导出 {len(pngs)} 张 PNG → {args.out_dir}")
    return 0


def _cmd_llmping(_args) -> int:
    import time as _time

    from pydantic import BaseModel

    from app.config import get_settings
    from app.llm.client import LLMClient, LLMError

    class Ping(BaseModel):
        ok: bool
        message: str

    try:
        client = LLMClient()
    except LLMError as e:
        print(f"[fail] {e}")
        return 1

    s = get_settings()
    print(f"base_url={s.llm_base_url}  model={s.llm_model}")

    t0 = _time.monotonic()
    try:
        text = client.chat([{"role": "user", "content": "只回复一个词：pong"}], stage="llmping")
        print(f"[chat ] ok  {(_time.monotonic() - t0):.2f}s  reply={text[:60]!r}")
    except Exception as e:
        print(f"[chat ] FAIL  {e}")
        return 1

    t0 = _time.monotonic()
    try:
        obj = client.structured(
            Ping,
            [{"role": "user", "content": "连通性测试：ok 填 true，message 填一句话确认。"}],
            stage="llmping-structured",
        )
        print(f"[json ] ok  {(_time.monotonic() - t0):.2f}s  mode={client.json_mode}  parsed={obj.model_dump()}")
    except Exception as e:
        print(f"[json ] FAIL  {e}")
        return 1

    print("llmping 通过")
    return 0


def _cmd_run(args) -> int:
    import time as _time
    from pathlib import Path

    from app.parsing.base import ParseError
    from app.services.jobs import JobError, JobManager

    source = Path(args.source)
    if not source.exists():
        print(f"[fail] 文件不存在：{source}")
        return 1
    if args.genre and args.product != "doc":
        print("[fail] --genre 仅用于 --product doc")
        return 1
    if args.genre and not args.auto_confirm:
        print("[fail] --genre 需配合 --auto-confirm；或改用："
              f"python -m app confirm <job_id> --genre {args.genre}")
        return 1

    mgr = JobManager()
    try:
        # --genre 时不走 auto_confirm：等 PLANNED 落盘 docplan 后带体裁确认
        job = mgr.create(source, skin=args.skin, product=args.product,
                         auto_confirm=args.auto_confirm and not args.genre)
    except (ParseError, JobError) as e:
        print(f"[fail] {e}")
        return 1

    print(f"job_id={job.job_id}")
    if args.genre:
        while job.status.value not in ("PLANNED", "FAILED"):
            _time.sleep(0.5)
        if job.status.value == "FAILED":
            print(f"失败：{job.error}")
            return 1
        try:
            job = mgr.confirm(job.job_id, genre=args.genre)
        except JobError as e:
            print(f"[fail] {e}")
            return 1
        print(f"已按体裁 {args.genre} 确认，流水线继续。")

    if not job.confirmed:
        print("流水线将在大纲确认点（PLANNED）暂停。确认命令：")
        suffix = " --genre letter|report|form" if args.product == "doc" else ""
        print(f"  python -m app confirm {job.job_id}{suffix}")
        print(f"查看状态：python -m app status {job.job_id}")
        return 0

    last = None
    while job.status.value not in ("DONE", "FAILED"):
        _time.sleep(1)
        cur = (job.status.value, job.detail)
        if cur != last:
            print(f"[{cur[0]:<11}] {cur[1]}")
            last = cur
    d = job.to_dict()
    if job.status.value == "DONE":
        if args.product == "doc":
            print(f"完成：{d['output_docx']}")
            print(f"PDF：{d['output_pdf']}")
        else:
            print(f"完成：{d['output_pptx']}")
        print(f"页面预览：{job.dir / 'pages'}")
        return 0
    print(f"失败：{job.error}")
    return 1


def _cmd_confirm(args) -> int:
    from app.services.jobs import JobError, JobManager

    try:
        job = JobManager().confirm(args.job_id, genre=args.genre)
    except JobError as e:
        print(f"[fail] {e}")
        return 1
    print(f"已确认 {job.job_id}，流水线继续。")
    return 0


def _cmd_status(args) -> int:
    from app.services.jobs import JobError, JobManager

    mgr = JobManager()
    try:
        jobs = [mgr.get(args.job_id)] if args.job_id else None
    except JobError as e:
        print(f"[fail] {e}")
        return 1
    for d in ([j.to_dict() for j in jobs] if jobs else mgr.list()):
        err = f"  error: {d['error']}" if d["error"] else ""
        print(f"{d['job_id']}  {d['status']:<10} {d['detail']}{err}")
    return 0


def _cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(prog="app", description="AIGC 文档仿写 PPT Agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_render = sub.add_parser("render", help="SlideIR/DocIR JSON → pptx/docx（调试用）")
    p_render.add_argument("deck", help="SlideIR deck 或 DocIR JSON 路径")
    p_render.add_argument("--skin", default="business_blue")
    p_render.add_argument("--out", required=True, help="输出 pptx / docx 路径")
    p_render.add_argument("--pngs", metavar="DIR", help="同时 COM 导出 PNG 到该目录（pptx）")
    p_render.add_argument("--force", action="store_true", help="schema 校验失败仍继续渲染")
    p_render.add_argument("--docir", action="store_true", help="输入按 DocIR 解析，渲染 docx")
    p_render.add_argument("--pdf", action="store_true", help="docx 再经 Word COM 转 PDF（--docir）")
    p_render.set_defaults(func=_cmd_render)

    p_export = sub.add_parser("export", help="已有 pptx → PNG")
    p_export.add_argument("pptx")
    p_export.add_argument("--out-dir", required=True)
    p_export.set_defaults(func=_cmd_export)

    sub.add_parser("llmping", help="360智脑连通性 smoke（W2）").set_defaults(func=_cmd_llmping)

    p_run = sub.add_parser("run", help="源文档 → PPT / Word·PDF 文档全流程")
    p_run.add_argument("source")
    p_run.add_argument("--skin", default="business_blue")
    p_run.add_argument("--product", choices=("ppt", "doc"), default="ppt")
    p_run.add_argument("--genre", choices=("letter", "report", "form"),
                       help="文档体裁（仅 --product doc，需 --auto-confirm）")
    p_run.add_argument("--auto-confirm", action="store_true", help="跳过大纲确认（测试用）")
    p_run.set_defaults(func=_cmd_run)

    p_confirm = sub.add_parser("confirm", help="确认 PLANNED 状态任务的大纲")
    p_confirm.add_argument("job_id")
    p_confirm.add_argument("--genre", choices=("letter", "report", "form"),
                           help="文档任务可同时改体裁")
    p_confirm.set_defaults(func=_cmd_confirm)

    p_status = sub.add_parser("status", help="查看任务状态")
    p_status.add_argument("job_id", nargs="?")
    p_status.set_defaults(func=_cmd_status)

    p_serve = sub.add_parser("serve", help="启动 Web UI（上传/进度/预览/下载）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
