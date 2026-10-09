from __future__ import annotations

from pydantic import BaseModel, Field

SLOT_ROLES = ("title", "body", "label", "desc", "page_no", "design")
ROLE_HINTS = ("cover", "toc", "section", "content", "closing", "table_host", "")


class SlotInfo(BaseModel):
    """模板页内一个可填充文本槽。

    slot_id/path 由 shape 树索引路径构成（如 "0.1.1.0.0"），目录提取与
    构建替换共用同一枚举顺序（_shape_children），保证寻址不漂移。
    """

    slot_id: str
    path: list[int]
    role: str  # SLOT_ROLES 之一
    x: float  # 英寸；组仿射变换已解析为页面绝对坐标
    y: float
    w: float
    h: float
    font_pt: float  # run sz → 段落 defRPr → 版式占位符 → 18.0 兜底
    sample: str = ""  # 模板原文样本（≤40 字符）
    capacity: float  # 汉字当量

    @property
    def fillable(self) -> bool:
        return self.role not in ("design", "page_no") and self.capacity >= 3.0


class SlideInfo(BaseModel):
    slide_no: int  # sldIdLst 顺序（1 起）
    layout: str
    signature: str  # 紧凑摘要，供规划 prompt 与确认页展示
    slots: list[SlotInfo] = Field(default_factory=list)
    fillable: int = 0  # 非 design/page_no 且容量 ≥3 的槽数
    fillable_cap: float = 0.0  # 可填槽的最大容量（区分"常规内容页"与"表格宿主页"）
    role_hint: str = ""  # ROLE_HINTS 之一

    def slot(self, slot_id: str) -> SlotInfo | None:
        for s in self.slots:
            if s.slot_id == slot_id:
                return s
        return None

    def slots_of_role(self, *roles: str) -> list[SlotInfo]:
        return [s for s in self.slots if s.role in roles]


class TemplateCatalog(BaseModel):
    stem: str
    display_name: str
    n_slides: int
    slides: list[SlideInfo]

    def slide(self, slide_no: int) -> SlideInfo:
        """1 起页号；越界是上游校验漏洞，直接抛。"""
        for s in self.slides:
            if s.slide_no == slide_no:
                return s
        raise IndexError(f"模板 {self.stem} 无第 {slide_no} 页（共 {self.n_slides} 页）")
