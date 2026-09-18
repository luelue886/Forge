from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.schema.doctree import DocTree
from app.schema.enums import Genre
from pydantic import BaseModel

_SALUTATION = re.compile(
    r"^(?:尊敬的|亲爱的|敬爱的|各位|尊敬的各位)\S{0,20}[：:]$")
_CLOSING_LINE = re.compile(r"^此致(?:\s*敬礼)?[！!？?]?$")
_REVERENCE = re.compile(r"^敬礼[！!？?]?$")
_DATE_ALONE = re.compile(
    r"^(?:\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})"
    r"[日号]?$")
_LETTER_TITLE = re.compile(
    r"(?:申请书|感谢信|道歉信|请假条|倡议书|求职信|介绍信|慰问信|贺信|决心书|"
    r"保证书|检讨书|邀请函|聘请书|委托书|请示|公函|书信|回信|回函)")
_REPORT_TITLE = re.compile(
    r"(?:报告|总结|方案|意见|说明|分析|汇报|计划|规划|纪要|简报|通报|调研|评估)")

LLM_CONFIDENCE = 0.5  # 规则置信度低于该值才请 LLM 兜底


@dataclass
class LetterFrame:
    """书信族确定性框架，全部 verbatim 抽取，不经 LLM。"""

    salutation: str | None = None
    closing: list[str] = field(default_factory=list)
    signer: str | None = None
    date: str | None = None


@dataclass
class GenreScore:
    genre: Genre
    confidence: float
    reasons: list[str] = field(default_factory=list)


def _texts_in_order(tree: DocTree) -> list[str]:
    out = []
    for sec in tree.sections[0].walk():
        for b in sec.blocks:
            if b.kind in ("para", "list_item") and b.text.strip():
                out.append(b.text.strip())
    return out


def detect_genre(tree: DocTree) -> GenreScore:
    """规则打分：letter / report / form 三选一。置信度 < 0.5 时调用方应走 LLM 兜底。"""
    root = tree.sections[0]
    title = root.title or ""
    texts = _texts_in_order(tree)
    reasons: list[str] = []

    salutation = next((t for t in texts[:5] if _SALUTATION.match(t)), None)
    closing_idx = next((i for i, t in enumerate(texts) if _CLOSING_LINE.match(t)), None)
    date_tail = bool(texts and _DATE_ALONE.match(texts[-1]))

    letter = 0.0
    if salutation:
        letter += 0.4
        reasons.append("开头称呼行")
    if closing_idx is not None:
        letter += 0.3
        reasons.append("此致敬礼")
    if date_tail:
        letter += 0.15
        reasons.append("末尾日期行")
    if _LETTER_TITLE.search(title):
        letter += 0.25
        reasons.append("标题关键词")

    report = 0.0
    n_headings = sum(1 for s in root.walk() if s.level >= 1)
    if _REPORT_TITLE.search(title):
        report += 0.5
        reasons.append("标题关键词")
    if n_headings >= 2:
        report += 0.2
        reasons.append("多级标题结构")
    if salutation is None and closing_idx is None:
        report += 0.1

    form = 0.0
    table_chars = sum(len(t.flat_text()) for t in tree.tables)
    total_chars = max(tree.meta.n_chars or len(tree.full_text), 1)
    ratio = table_chars / total_chars
    if ratio >= 0.5:
        form += 0.6
        reasons.append(f"表格占比 {ratio:.0%}")
    elif ratio >= 0.35:
        form += 0.3
        reasons.append(f"表格占比 {ratio:.0%}")
    if tree.tables and len(texts) <= 6:
        form += 0.15

    best = max((("letter", letter), ("report", report), ("form", form)),
               key=lambda kv: kv[1])
    genre = Genre(best[0])
    return GenreScore(genre=genre, confidence=min(best[1], 1.0), reasons=reasons)


def extract_letter_frame(tree: DocTree) -> LetterFrame:
    """从源文档 verbatim 抽取称呼/此致敬礼/落款（署名+日期）。"""
    texts = _texts_in_order(tree)
    frame = LetterFrame()

    for t in texts[:5]:
        if _SALUTATION.match(t):
            frame.salutation = t
            break

    for i, t in enumerate(texts):
        if _CLOSING_LINE.match(t):
            frame.closing = [t]
            if i + 1 < len(texts) and _REVERENCE.match(texts[i + 1]):
                frame.closing.append(texts[i + 1])
            break

    for i in range(len(texts) - 1, -1, -1):
        if _DATE_ALONE.match(texts[i]):
            frame.date = texts[i]
            for j in range(i - 1, max(i - 3, -1), -1):
                cand = texts[j]
                if (cand and len(cand) <= 30 and not _SALUTATION.match(cand)
                        and not _CLOSING_LINE.match(cand)
                        and not _REVERENCE.match(cand)):
                    frame.signer = cand
                    break
            break
    return frame


class GenreChoice(BaseModel):
    genre: Genre
    reason: str = ""


def classify_genre_llm(tree: DocTree, client: LLMClient,
                       pm: PromptManager | None = None,
                       score: GenreScore | None = None) -> Genre:
    """规则拿不准时的 LLM 三选一兜底。"""
    pm = pm or PromptManager()
    score = score or detect_genre(tree)
    rule_report = (f"规则判定 {score.genre.value}（置信度 {score.confidence:.2f}；"
                   f"{'；'.join(score.reasons) or '无显著特征'}）")
    user = pm.render("genre/user.j2",
                     doc_title=tree.sections[0].title or "（无标题）",
                     rule_report=rule_report,
                     excerpt=tree.full_text[:1500])
    messages = [
        {"role": "system", "content": pm.render("genre/system.md")},
        {"role": "user", "content": user},
    ]
    out = client.structured(GenreChoice, messages, stage="genre")
    return out.genre
