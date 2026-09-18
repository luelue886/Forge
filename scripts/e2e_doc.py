"""3 golden 样例 e2e：run --product doc --auto-confirm → DONE + 仿写硬断言。

断言（独立复检，不信任流水线自报）：
  1) 状态 DONE，qa_report 无 E- 级残留
  2) heading+para 数字 100% 溯源（check_numbers）
  3) heading+para 与源文 10-gram 零命中（ngram_hits）
  4) 表格 verbatim：DocIR TableBlock 与源 DocTable 程序 diff
  5) 产物齐套：output.docx / output.pdf / pages/*.png，PNG 数 == PDF 页数

用法：.venv/Scripts/python.exe scripts/e2e_doc.py [样例路径 ...]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = ROOT / ".venv" / "Scripts" / "python.exe"
GOLDEN = [
    ROOT / "examples" / "letter_application.docx",
    ROOT / "examples" / "sectioned_report.docx",
    ROOT / "examples" / "text_report.pdf",
]


def run_sample(src: Path) -> tuple[bool, list[str]]:
    from app.config import DATA_DIR
    from app.qa.ngram import ngram_hits
    from app.qa.numbers import check_numbers
    from app.schema.doctree import DocTree

    errors: list[str] = []
    r = subprocess.run(
        [str(PY), "-m", "app", "run", str(src), "--product", "doc", "--auto-confirm"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return False, [f"CLI 失败（rc={r.returncode}）：\n{out[-1500:]}"]

    job_id = next((l.split("=", 1)[1].strip() for l in out.splitlines()
                   if l.startswith("job_id=")), None)
    if not job_id:
        return False, [f"未解析到 job_id：\n{out[-500:]}"]
    job_dir = DATA_DIR / "jobs" / job_id
    art = job_dir / "artifacts"

    state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    if state["status"] != "DONE":
        return False, [f"状态 {state['status']}：{state.get('error')}"]

    docir = json.loads((art / "docir.json").read_text(encoding="utf-8"))
    tree = DocTree.model_validate_json(
        (art / "doctree.json").read_text(encoding="utf-8"))

    # 1) 数字 100% 溯源 + 2) 10-gram 零命中（heading+para 独立复检）
    gen = " ".join(b["text"] for b in docir["blocks"]
                   if b["kind"] in ("heading", "para"))
    for tok, ctx in check_numbers(gen, tree.full_text):
        errors.append(f"[E-NUM-UNTRACED] {tok}: {ctx}")
    for g in ngram_hits(gen, tree.full_text):
        errors.append(f"[E-PLAGIARISM] 与源文连续雷同：{g}")

    # 3) 表格 verbatim diff
    src_tables = {t.table_id: t for t in tree.tables}
    for b in docir["blocks"]:
        if b["kind"] != "table":
            continue
        t = src_tables.get(b["table_id"])
        if t is None:
            errors.append(f"[TABLE-DIFF] {b['table_id']} 不在源文档")
        elif b["header"] != list(t.header) or b["rows"] != [list(r) for r in t.rows]:
            errors.append(f"[TABLE-DIFF] {b['table_id']} 与源表不一致")

    # 4) QA 报告无 E- 级残留
    qa = (art / "qa_report.txt").read_text(encoding="utf-8")
    residuals = [l for l in qa.splitlines() if "[docqa]" in l]
    if residuals:
        errors.append(f"QA 残留 {len(residuals)} 项：\n" + "\n".join(residuals[:5]))

    # 5) 产物齐套
    docx, pdf = art / "output.docx", art / "output.pdf"
    if not docx.exists() or docx.stat().st_size < 4000:
        errors.append("output.docx 缺失或过小")
    if not pdf.exists() or pdf.stat().st_size < 4000:
        errors.append("output.pdf 缺失或过小")
    else:
        import pymupdf

        with pymupdf.open(str(pdf)) as d:
            n_pages = len(d)
        pngs = sorted((job_dir / "pages").glob("page_*.png"))
        if len(pngs) != n_pages:
            errors.append(f"预览 PNG {len(pngs)} 张 ≠ PDF {n_pages} 页")

    from docx import Document

    d = Document(str(docx))
    if len(d.paragraphs) < 3:
        errors.append(f"docx 段落数异常（{len(d.paragraphs)}）")

    return not errors, errors


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv] or GOLDEN
    ok_all = True
    for src in targets:
        if not src.exists():
            print(f"[skip] 样例不存在：{src}")
            ok_all = False
            continue
        print(f"\n===== {src.name} =====")
        ok, errs = run_sample(src)
        print("[PASS]" if ok else "[FAIL]")
        for e in errs:
            print(f"  {e}")
        ok_all = ok_all and ok
    print("\n" + ("全部通过" if ok_all else "存在失败"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
