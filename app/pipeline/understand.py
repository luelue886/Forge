from __future__ import annotations

import re

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.schema.docmap import DocMap, KeyFact, SectionMap
from app.schema.doctree import DocTree

SECTION_TEXT_LIMIT = 2000


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _table_stats_by_section(tree: DocTree) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in tree.tables:
        out.setdefault(t.section_id, []).append({
            "table_id": t.table_id,
            "n_rows": t.n_rows,
            "n_cols": t.n_cols,
            "header_preview": "、".join(t.header)[:40] or "（无表头）",
            "caption": t.caption or "",
        })
    return out


def verify_facts(sm: SectionMap, full_text: str, known_refs: set[str]) -> SectionMap:
    """代码回验：text 必须命中 full_text（容忍空白差异）；block_ids 必须是合法地址。"""
    flat = _squash(full_text)
    kept: list[KeyFact] = []
    for f in sm.key_facts:
        if not f.text:
            continue
        if f.text in full_text or _squash(f.text) in flat:
            f.block_ids = [b for b in f.block_ids if b in known_refs]
            kept.append(f)
    sm.key_facts = kept
    return sm


def understand(tree: DocTree, client: LLMClient, pm: PromptManager | None = None) -> DocMap:
    """逐节调用 LLM 产出 SectionMap，代码回验事实后聚合为 DocMap。"""
    pm = pm or PromptManager()
    known = tree.known_refs()
    stats = _table_stats_by_section(tree)
    root_title = tree.sections[0].title

    maps: list[SectionMap] = []
    for sec in tree.sections[0].walk():
        if not sec.blocks:
            continue
        user = pm.render(
            "docmap/user.j2",
            doc_title=root_title,
            section_title=sec.title,
            section_id=sec.section_id,
            level=sec.level,
            section_text=sec.text_of()[:SECTION_TEXT_LIMIT],
            table_stats=stats.get(sec.section_id, []),
        )
        messages = [
            {"role": "system", "content": pm.render("docmap/system.md")},
            {"role": "user", "content": user},
        ]
        sm = client.structured(SectionMap, messages, stage=f"docmap/{sec.section_id}")
        sm.section_id = sec.section_id  # 地址以代码为准，不信 LLM 回显
        maps.append(verify_facts(sm, tree.full_text, known))

    return DocMap(doc_title=root_title, section_maps=maps)
