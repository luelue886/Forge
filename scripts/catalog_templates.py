"""生成 PPT 模板目录（确定性，一次入库提交，运行时不重算）。

用法：
    python scripts/catalog_templates.py [stem ...] [--thumbs]

不带参数 = 对 templates/pptx/ 下全部 *.pptx 生成 <stem>.catalog.json。
--thumbs 额外用 COM 导出每页 PNG 到 templates/pptx/<stem>.pages/（UI 模板预览用）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.render.tpl_catalog import (  # noqa: E402
    PPTX_TEMPLATES_DIR, extract_catalog, save_catalog, template_pptx_path,
)

DISPLAY_NAMES = {
    "40页商务蓝绿色关系可视化图表集PPT模板": "商务蓝绿图表集（26 页）",
    "办公技巧商务培训PPT模板": "办公技巧商务培训（33 页）",
    "PPT模板": "安全教育课件（24 页）",
    "indie_bookstore_zine_guide": "独立书店杂志风（英文）",
    "sugar_rush_memphis": "甜品店孟菲斯风（英文）",
    "glassmorphism_demo": "玻璃拟态演示（英文）",
    "swiss_grid_systems": "瑞士网格系统（英文）",
    "global_ai_capital_2026": "AI 资本报告 2026（英文）",
    "pritzker_2026": "普利兹克 2026（英文）",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stems", nargs="*", help="模板 stem；缺省 = 全部")
    ap.add_argument("--thumbs", action="store_true", help="COM 导出逐页 PNG 预览")
    args = ap.parse_args()

    if not PPTX_TEMPLATES_DIR.exists():
        print(f"目录不存在：{PPTX_TEMPLATES_DIR}")
        return 1
    stems = args.stems or sorted(
        p.stem for p in PPTX_TEMPLATES_DIR.glob("*.pptx"))
    for stem in stems:
        pptx = template_pptx_path(stem)
        if not pptx.exists():
            print(f"跳过 {stem}：无 {pptx.name}")
            continue
        cat = extract_catalog(pptx, DISPLAY_NAMES.get(stem))
        path = save_catalog(cat)
        n_slots = sum(len(s.slots) for s in cat.slides)
        print(f"{stem}: {cat.n_slides} 页 / {n_slots} 槽 → {path.name}")
        if args.thumbs:
            from app.services.com_export import export_pngs
            out = PPTX_TEMPLATES_DIR / f"{stem}.pages"
            pngs = export_pngs(pptx, out)
            print(f"  缩略图 {len(pngs)} 张 → {out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
