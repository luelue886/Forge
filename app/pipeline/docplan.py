from __future__ import annotations

import logging

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.pipeline.genre import GenreScore, LLM_CONFIDENCE, classify_genre_llm, detect_genre
from app.schema.docplan import DocPlan, DocPlanItem
from app.schema.doctree import DocTree
from app.schema.enums import Genre

log = logging.getLogger(__name__)


def resolve_genre(tree: DocTree, client: LLMClient | None,
                  pm: PromptManager | None = None) -> tuple[Genre, GenreScore]:
    """规则先行；置信度不足且有 client 时 LLM 兜底（失败回落规则结果）。"""
    score = detect_genre(tree)
    if score.confidence >= LLM_CONFIDENCE or client is None:
        return score.genre, score
    try:
        genre = classify_genre_llm(tree, client, pm, score)
        return genre, score
    except Exception as e:  # noqa: BLE001 — LLM 失败不阻塞，回落规则
        log.warning("体裁 LLM 兜底失败，沿用规则结果 %s：%s", score.genre.value, e)
        return score.genre, score


def build_doc_plan(tree: DocTree, client: LLMClient | None = None,
                   pm: PromptManager | None = None) -> DocPlan:
    """确定性规划：跟随源结构，每节一个 item；体裁识别规则先行。"""
    genre, _score = resolve_genre(tree, client, pm)
    root = tree.sections[0]

    tables_by_sec: dict[str, list[str]] = {}
    for t in tree.tables:
        tables_by_sec.setdefault(t.section_id, []).append(t.table_id)
    images_by_sec: dict[str, list[str]] = {}
    for im in tree.images:
        images_by_sec.setdefault(im.section_id, []).append(im.image_id)

    items: list[DocPlanItem] = []
    if root.blocks:
        items.append(DocPlanItem(
            seq=1, section_id=root.section_id, heading="",
            heading_level=0, src_refs=[root.section_id],
            table_ids=tables_by_sec.get(root.section_id, []),
            image_ids=images_by_sec.get(root.section_id, []),
        ))
    for sec in root.walk():
        if sec is root or sec.section_id == root.section_id:
            continue
        if not sec.blocks and not sec.subsections:
            continue  # 空章节（解析噪声）不进规划
        items.append(DocPlanItem(
            seq=len(items) + 1, section_id=sec.section_id,
            heading=sec.title, heading_level=min(sec.level, 2),
            src_refs=[sec.section_id],
            table_ids=tables_by_sec.get(sec.section_id, []),
            image_ids=images_by_sec.get(sec.section_id, []),
        ))

    if not items:  # 全空文档兜底：至少一个根 item
        items.append(DocPlanItem(seq=1, section_id=root.section_id, heading="",
                                 heading_level=0,
                                 src_refs=[root.section_id], table_ids=[]))

    return DocPlan(genre=genre, title=root.title or "未命名文档", items=items)
