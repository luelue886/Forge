from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from app.schema.enums import Genre, IssueCode
from app.schema.textlen import text_weight

SCHEMA_VERSION = "docir/1.0"


class DocIssue(BaseModel):
    rule: IssueCode
    detail: str
    seq: int | None = None  # 关联块下标（0 基），供定向修复定位


# ---- 块模型（discriminated union，blocks 列表顺序即文档顺序）----

class DocTitleBlock(BaseModel):
    kind: Literal["doc_title"] = "doc_title"
    text: str


class HeadingBlock(BaseModel):
    kind: Literal["heading"] = "heading"
    level: int  # 1|2
    text: str


class ParaBlock(BaseModel):
    kind: Literal["para"] = "para"
    text: str


class SalutationBlock(BaseModel):
    kind: Literal["salutation"] = "salutation"
    text: str  # verbatim，如“尊敬的各位领导：”


class ClosingBlock(BaseModel):
    kind: Literal["closing"] = "closing"
    lines: list[str]  # verbatim，如 ["此致", "敬礼！"]


class SignatureBlock(BaseModel):
    kind: Literal["signature"] = "signature"
    signer: str  # verbatim
    date: str = ""  # verbatim，可空


class TableBlock(BaseModel):
    kind: Literal["table"] = "table"
    table_id: str  # 溯源锚点
    header: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)  # verbatim（C5 起长文本格可改写）
    src_index: int | None = None   # 透传 DocTable：源 docx 表格序号（XML 搬运用）
    col_widths: list[float] | None = None  # 透传：无源可搬时的列宽比例


class ImageBlock(BaseModel):
    """图片/流程图原样复用：内容永不经 LLM，渲染期从源文件搬运。"""

    kind: Literal["image"] = "image"
    image_id: str  # 溯源锚点
    body_index: int | None = None  # docx 源：body 子元素序号（deepcopy 锚点）
    page: int | None = None  # pdf 源：1 基页码
    bbox: list[float] | None = None  # pdf 源：页面区域 (x0, top, x1, bottom)
    cx_emu: int | None = None
    cy_emu: int | None = None


DocIRBlock = Annotated[
    Union[DocTitleBlock, HeadingBlock, ParaBlock, SalutationBlock,
          ClosingBlock, SignatureBlock, TableBlock, ImageBlock],
    Field(discriminator="kind"),
]


class DocIRMeta(BaseModel):
    title: str
    genre: Genre
    language: str = "zh-CN"


class DocIR(BaseModel):
    schema_version: Literal["docir/1.0"] = SCHEMA_VERSION
    meta: DocIRMeta
    blocks: list[DocIRBlock]


class SectionIR(BaseModel):
    """逐节 fill 的落盘产物；assembly 按 seq 拼进 DocIR.blocks。"""

    section_id: str
    blocks: list[DocIRBlock] = Field(default_factory=list)


GENRE_BLOCKS: dict[Genre, frozenset[str]] = {
    Genre.LETTER: frozenset({"doc_title", "para", "salutation", "closing", "signature", "table", "image"}),
    Genre.REPORT: frozenset({"doc_title", "heading", "para", "table", "image"}),
    Genre.FORM: frozenset({"doc_title", "heading", "para", "table", "image"}),
}

DOC_LIMITS = {
    "title_weight": 30.0,
    "heading_weight": 25.0,
    "para_weight_warn": 400.0,  # 流式排版不阻断，仅警告建议拆段
    "form_max_paras": 6,
}


def _block_empty(b) -> bool:
    if isinstance(b, (DocTitleBlock, HeadingBlock, ParaBlock, SalutationBlock)):
        return not b.text.strip()
    if isinstance(b, ClosingBlock):
        return not any(l.strip() for l in b.lines)
    if isinstance(b, SignatureBlock):
        return not (b.signer.strip() or b.date.strip())
    if isinstance(b, TableBlock):
        return not (b.header or b.rows)
    return False


def validate_docir(doc: DocIR) -> list[DocIssue]:
    issues: list[DocIssue] = []

    def add(rule: IssueCode, detail: str, seq: int | None = None) -> None:
        issues.append(DocIssue(rule=rule, detail=detail, seq=seq))

    blocks = doc.blocks
    kinds = [b.kind for b in blocks]
    genre = doc.meta.genre
    L = DOC_LIMITS

    # doc_title：恰好 1 个且为首块
    title_idx = [i for i, k in enumerate(kinds) if k == "doc_title"]
    if len(title_idx) != 1:
        add(IssueCode.V_DOC_STRUCTURE, f"doc_title 必须 1 个，实际 {len(title_idx)} 个")
    elif title_idx[0] != 0:
        add(IssueCode.V_DOC_STRUCTURE, "doc_title 必须是首块", 0)

    # 空块
    for i, b in enumerate(blocks):
        if _block_empty(b):
            add(IssueCode.V_DOC_EMPTY, f"块 {i}（{b.kind}）为空", i)

    # 长度硬规则
    for i, b in enumerate(blocks):
        if isinstance(b, DocTitleBlock) and text_weight(b.text) > L["title_weight"]:
            add(IssueCode.V_DOC_TITLE_LEN,
                f"文档标题 {text_weight(b.text):.0f} 汉字当量超过 {L['title_weight']:.0f}", i)
        if isinstance(b, HeadingBlock) and text_weight(b.text) > L["heading_weight"]:
            add(IssueCode.V_DOC_HEADING_LEN,
                f"标题 {text_weight(b.text):.0f} 汉字当量超过 {L['heading_weight']:.0f}：{b.text[:15]}…", i)

    # 体裁块白名单
    allowed = GENRE_BLOCKS[genre]
    for i, k in enumerate(kinds):
        if k not in allowed:
            add(IssueCode.V_DOC_GENRE_BLOCKS,
                f"体裁 {genre.value} 不允许块 {k}（块 {i}）", i)

    # letter 框架顺序
    if genre is Genre.LETTER:
        if kinds.count("salutation") > 1:
            add(IssueCode.V_DOC_STRUCTURE, "salutation 最多 1 个")
        elif "salutation" in kinds and kinds.index("salutation") != 1:
            add(IssueCode.V_DOC_STRUCTURE, "salutation 必须紧跟 doc_title",
                kinds.index("salutation"))
        for k in ("closing", "signature"):
            if kinds.count(k) > 1:
                add(IssueCode.V_DOC_STRUCTURE, f"{k} 最多 1 个")
        if "closing" in kinds and "signature" in kinds \
                and kinds.index("signature") < kinds.index("closing"):
            add(IssueCode.V_DOC_STRUCTURE, "signature 必须在 closing 之后",
                kinds.index("signature"))
        body_last = max((i for i, k in enumerate(kinds) if k in ("para", "heading", "table")),
                        default=-1)
        for k in ("closing", "signature"):
            if k in kinds and kinds.index(k) <= body_last:
                add(IssueCode.V_DOC_STRUCTURE, f"{k} 必须位于全部正文块之后",
                    kinds.index(k))

    # heading 级别 ∈{1,2} 且不跳级
    last_level = 0
    for i, b in enumerate(blocks):
        if isinstance(b, HeadingBlock):
            if b.level not in (1, 2):
                add(IssueCode.V_DOC_STRUCTURE, f"heading level 必须为 1 或 2（块 {i}）", i)
            elif b.level > last_level + 1:
                add(IssueCode.V_DOC_STRUCTURE, f"heading 跳级（块 {i}，level {b.level}）", i)
            last_level = b.level

    # form 体裁：表格为主
    if genre is Genre.FORM:
        n_tables = kinds.count("table")
        n_paras = kinds.count("para")
        if n_tables < 1:
            add(IssueCode.V_DOC_STRUCTURE, "form 体裁必须至少包含 1 个表格")
        if n_paras > L["form_max_paras"]:
            add(IssueCode.V_DOC_STRUCTURE,
                f"form 体裁 para {n_paras} 条超过 {L['form_max_paras']}")

    # para 超长（仅警告）
    for i, b in enumerate(blocks):
        if isinstance(b, ParaBlock) and text_weight(b.text) > L["para_weight_warn"]:
            add(IssueCode.W_DOC_PARA_LONG,
                f"para {text_weight(b.text):.0f} 汉字当量超过 {L['para_weight_warn']:.0f}，建议拆段", i)

    return issues


def validate_section_ir(sec: SectionIR, genre: Genre) -> list[DocIssue]:
    """单节 fill 落盘前的可重试校验（块级规则）。

    跨节结构规则（doc_title 位置、heading 跳级、form 全文表格数、letter 框架顺序）
    由组装后的 validate_docir 把关。
    """
    issues: list[DocIssue] = []
    allowed = GENRE_BLOCKS[genre]
    for i, b in enumerate(sec.blocks):
        if _block_empty(b):
            issues.append(DocIssue(rule=IssueCode.V_DOC_EMPTY,
                                   detail=f"块 {i}（{b.kind}）为空", seq=i))
        if b.kind not in allowed:
            issues.append(DocIssue(rule=IssueCode.V_DOC_GENRE_BLOCKS,
                                   detail=f"体裁 {genre.value} 不允许块 {b.kind}（块 {i}）", seq=i))
        if isinstance(b, HeadingBlock) and text_weight(b.text) > DOC_LIMITS["heading_weight"]:
            issues.append(DocIssue(
                rule=IssueCode.V_DOC_HEADING_LEN,
                detail=f"标题 {text_weight(b.text):.0f} 汉字当量超过 "
                       f"{DOC_LIMITS['heading_weight']:.0f}：{b.text[:15]}…", seq=i))
        if isinstance(b, ParaBlock) and text_weight(b.text) > DOC_LIMITS["para_weight_warn"]:
            issues.append(DocIssue(
                rule=IssueCode.W_DOC_PARA_LONG,
                detail=f"para {text_weight(b.text):.0f} 汉字当量超过 "
                       f"{DOC_LIMITS['para_weight_warn']:.0f}，建议拆段", seq=i))
    if not any(b.kind == "para" for b in sec.blocks):
        issues.append(DocIssue(rule=IssueCode.V_DOC_EMPTY, detail="节内没有正文段落"))
    return issues
