"""PPT 线 golden 样例 e2e：run --template <stem> --auto-confirm → DONE + 仿写硬断言。

断言（独立复检，不信任流水线自报）：
  1) 状态 DONE，qa_report 无 E- 级残留
  2) 数字 100% 溯源（check_numbers）：所有页所有填充槽的文本
  3) 10-gram 防抄袭（ngram_hits）：仅内容页（TEXT_POINTS/TWO_COLUMN/
     KEY_METRICS）的填充槽——结构页（封面/目录/节题/尾页）N2 起 LLM 生成，
     标题/条目/文档题合法回源文标题，不做改写义美（与 slotfill 复检口径
     一致）；表格页 verbatim 生成；备注允许引用原文，跳过。
     事实跨度（docmap key_facts，铁律 verbatim）比对前剥离——两律相撞
     不算抄袭，对齐文档线 check-before-restore 语义
  4) 遗留检查独立复跑（template_checklist）：零问题
  5) 产物齐套：output.pptx、pages/page_NN.fill.json 全套、PNG 数 == 页数
  6) 样例横跨中文模板与英文成品模板

用法：.venv/Scripts/python.exe scripts/e2e_ppt.py [源文件::模板stem ...]
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台/管道默认 GBK：打印 CLI 捕获输出（errors="replace" 产生的
# U+FFFD）会以 UnicodeEncodeError 崩掉整个 e2e——统一重包为 UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)  # worktree 运行：用主仓库 venv 或当前解释器

# (源文档, 模板 stem) golden 对：中文模板 + 英文成品各至少 1
GOLDEN_PAIRS: list[tuple[str, str]] = [
    ("multi_table_report.docx", "办公技巧商务培训PPT模板"),
    ("real_finance_resume.pdf", "swiss_grid_systems"),
    ("sectioned_report.docx", "PPT模板"),
]


def run_sample(src: Path, stem: str) -> tuple[bool, list[str]]:
    from app.config import DATA_DIR
    from app.pipeline.slotfill import CONTENT_TYPES
    from app.qa.ngram import exempt_from_tree, ngram_hits, strip_facts
    from app.qa.numbers import check_numbers
    from app.render.tpl_catalog import load_catalog
    from app.schema.doctree import DocTree
    from app.schema.slotfill import DeckBuild
    from app.services.com_export import template_checklist

    errors: list[str] = []
    r = subprocess.run(
        [str(PY), "-m", "app", "run", str(src), "--template", stem, "--auto-confirm"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=3000)
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

    tree = DocTree.model_validate_json(
        (art / "doctree.json").read_text(encoding="utf-8"))
    exempt = exempt_from_tree(tree)
    # key_facts = 铁律 verbatim 的事实跨度：ngram 前剥离（两律相撞不算抄袭）
    docmap = json.loads((art / "docmap.json").read_text(encoding="utf-8"))
    facts = {f["text"] for sm in docmap.get("section_maps", [])
             for f in sm.get("key_facts", []) if f.get("text")}

    deck = DeckBuild.model_validate_json(
        (art / "deckbuild.json").read_text(encoding="utf-8"))
    catalog = load_catalog(stem)

    # 1) QA 报告无 E- 级残留
    qa = (art / "qa_report.txt").read_text(encoding="utf-8")
    bad = [l for l in qa.splitlines() if " E-" in l]
    if bad:
        errors.append(f"QA 残留 E- {len(bad)} 项：" + "; ".join(bad[:5]))

    # 2)+3) 逐页逐槽：数字溯源全覆盖；内容页 ngram 防抄袭
    for page in deck.pages:
        for sid, text in sorted(page.slot_values.items()):
            label = f"p{page.page_no} {sid}"
            for tok, ctx in check_numbers(text, tree.full_text):
                errors.append(f"[E-NUM-UNTRACED] {label} {tok}: {ctx}")
            if page.slide_type in CONTENT_TYPES:
                for g in ngram_hits(strip_facts(text, facts), tree.full_text,
                                    exempt=exempt):
                    errors.append(f"[E-PLAGIARISM] {label} 与源文连续雷同：{g}")
        if page.table is not None:
            for r_i, row in enumerate([page.table.header] + page.table.rows):
                for c_i, cell in enumerate(row):
                    for tok, ctx in check_numbers(cell, tree.full_text):
                        errors.append(
                            f"[E-NUM-UNTRACED] p{page.page_no} 表 {r_i},{c_i} {tok}: {ctx}")

    # 4) 遗留检查独立复跑（template_checklist 零问题）
    pptx = art / "output.pptx"
    residual = template_checklist(deck, catalog, pptx)
    if residual:
        errors.append(f"遗留检查复跑 {len(residual)} 项：\n" + "\n".join(
            f"  p{q.page_no} {q.code.value}: {q.detail}" for q in residual[:5]))

    # 5) 产物齐套：output.pptx、fill JSON 全套、PNG == 页数
    if not pptx.exists() or pptx.stat().st_size < 4000:
        errors.append("output.pptx 缺失或过小")
    pages_dir = job_dir / "pages"
    for page in deck.pages:
        if not (pages_dir / f"page_{page.page_no:02d}.fill.json").exists():
            errors.append(f"p{page.page_no} 缺 page_NN.fill.json 断点产物")
    pngs = sorted(pages_dir.glob("page_*.png"))
    if len(pngs) != len(deck.pages):
        errors.append(f"预览 PNG {len(pngs)} 张 ≠ deck {len(deck.pages)} 页")

    return not errors, errors


def main(argv: list[str]) -> int:
    pairs = [(Path(a.split("::")[0]), a.split("::")[1]) for a in argv] \
        if argv else [(ROOT / "examples" / s, t) for s, t in GOLDEN_PAIRS]
    ok_all = True
    for src, stem in pairs:
        if not src.exists():
            print(f"[skip] 样例不存在：{src}")
            ok_all = False
            continue
        print(f"\n===== {src.name} × {stem} =====")
        ok, errs = run_sample(src, stem)
        print("[PASS]" if ok else "[FAIL]")
        for e in errs:
            print(f"  {e}")
        ok_all = ok_all and ok
    print("\n" + ("全部通过" if ok_all else "存在失败"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
