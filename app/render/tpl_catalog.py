from __future__ import annotations

import json
import math
import re
import zipfile
from pathlib import Path

from lxml import etree

from app.config import PPTX_TEMPLATES_DIR
from app.schema.tplcatalog import SlideInfo, SlotInfo, TemplateCatalog
from app.schema.textlen import text_weight

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

_EMU_IN = 914400.0
_DEFAULT_FONT_PT = 18.0
_SAMPLE_MAX = 40

# 目录提取与构建替换共用的 shape 枚举：spTree/grpSp 的 shape 类子元素按文档序编号。
# slot_id/path 即此编号路径，两处必须 import 同一函数，寻址才不漂移。
_SHAPE_TAGS = frozenset(
    f"{{{NS['p']}}}{t}" for t in ("sp", "grpSp", "pic", "graphicFrame", "cxnSp")
)
_SP = f"{{{NS['p']}}}sp"
_GRP = f"{{{NS['p']}}}grpSp"

_PAGE_NO_RE = re.compile(r"^[\d\s./\-—页P·:]+$")
_HAS_CJK = re.compile("[一-鿿]")
_TITLE_TYPES = frozenset(("title", "ctrTitle"))

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


def shape_children(el) -> list:
    return [c for c in el if c.tag in _SHAPE_TAGS]


def _parse(xml_bytes: bytes):
    return etree.fromstring(xml_bytes, parser=_PARSER)


# ---------- rels ----------

def _read_rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """part（如 ppt/slides/slide1.xml）的 rels：rId → 仓库内绝对部件路径。"""
    rels_path = part.rsplit("/", 1)
    rels_path = f"{rels_path[0]}/_rels/{rels_path[1]}.rels"
    if rels_path not in z.namelist():
        return {}
    root = _parse(z.read(rels_path))
    base = part.rsplit("/", 1)[0]
    out: dict[str, str] = {}
    for rel in root.findall(f"{{{_REL}}}Relationship"):
        rid, target = rel.get("Id"), rel.get("Target", "")
        if not rid:
            continue
        if target.startswith("/"):
            resolved = target.lstrip("/")
        else:
            parts: list[str] = []
            for seg in f"{base}/{target}".split("/"):
                if seg in ("", "."):
                    continue
                if seg == "..":
                    if parts:
                        parts.pop()
                    continue
                parts.append(seg)
            resolved = "/".join(parts)
        out[rid] = resolved
    return out


# ---------- 字号链 ----------

def _sz_to_pt(sz: str | None) -> float | None:
    if not sz:
        return None
    try:
        return int(sz) / 100.0
    except ValueError:
        return None


def _explicit_font_pt(tx_body) -> float | None:
    for r in tx_body.iter(f"{{{NS['a']}}}r"):
        rPr = r.find(f"{{{NS['a']}}}rPr")
        pt = _sz_to_pt(rPr.get("sz")) if rPr is not None else None
        if pt:
            return pt
    for defRPr in tx_body.iter(f"{{{NS['a']}}}defRPr"):
        pt = _sz_to_pt(defRPr.get("sz"))
        if pt:
            return pt
    return None


def _ph_key(sp_el) -> tuple[str, str]:
    ph = sp_el.find(f"{{{NS['p']}}}nvSpPr/{{{NS['p']}}}nvPr/{{{NS['p']}}}ph")
    if ph is None:
        return "", ""
    return ph.get("type", ""), ph.get("idx", "")


def _iter_matching_sps(xml: bytes, ph_type: str, ph_idx: str):
    """版式/母版中与 (ph_type, ph_idx) 对应的占位符 sp。title/ctrTitle 视为同类。"""
    root = _parse(xml)
    for sp in root.iter(f"{{{NS['p']}}}sp"):
        t, i = _ph_key(sp)
        if not t and not i:
            continue
        if ph_type:
            if t == ph_type or (ph_type in _TITLE_TYPES and t in _TITLE_TYPES):
                yield sp
                continue
        if ph_idx and i == ph_idx:
            yield sp


def _chain_lookup(xmls: list, ph_type: str, ph_idx: str, extractor):
    """沿 版式 → 母版 链取首个命中（几何或字号）。"""
    for xml in xmls:
        if xml is None:
            continue
        for sp in _iter_matching_sps(xml, ph_type, ph_idx):
            v = extractor(sp)
            if v is not None:
                return v
    return None


def _sp_box_of(sp):
    return _sp_box(sp)


def _sp_font_of(sp):
    tx = sp.find(f"{{{NS['p']}}}txBody")
    return _explicit_font_pt(tx) if tx is not None else None


# ---------- 槽提取 ----------

class _Ctx:
    """子坐标空间 → 页面绝对坐标：abs = base + (x − choff) * scale。"""

    __slots__ = ("base", "choff", "scale")

    def __init__(self, base=(0.0, 0.0), choff=(0.0, 0.0), scale=(1.0, 1.0)):
        self.base = base
        self.choff = choff
        self.scale = scale

    def abs_box(self, x: float, y: float, cx: float, cy: float):
        ax = self.base[0] + (x - self.choff[0]) * self.scale[0]
        ay = self.base[1] + (y - self.choff[1]) * self.scale[1]
        return ax, ay, cx * self.scale[0], cy * self.scale[1]

    def child_ctx(self, off, ext, ch_off, ch_ext) -> "_Ctx":
        gx, gy, gw, gh = self.abs_box(*off, *ext)
        sx = (ext[0] / ch_ext[0]) if ch_ext[0] else 1.0
        sy = (ext[1] / ch_ext[1]) if ch_ext[1] else 1.0
        return _Ctx(base=(gx, gy), choff=ch_off, scale=(sx * self.scale[0], sy * self.scale[1]))


def _group_xfrm(grp):
    xfrm = grp.find(f"{{{NS['p']}}}grpSpPr/{{{NS['a']}}}xfrm")
    if xfrm is None:
        return None
    off = xfrm.find(f"{{{NS['a']}}}off")
    ext = xfrm.find(f"{{{NS['a']}}}ext")
    ch_off = xfrm.find(f"{{{NS['a']}}}chOff")
    ch_ext = xfrm.find(f"{{{NS['a']}}}chExt")
    if off is None or ext is None:
        return None
    return (
        (float(off.get("x", 0)), float(off.get("y", 0))),
        (float(ext.get("cx", 0)), float(ext.get("cy", 0))),
        (float(ch_off.get("x", 0)) if ch_off is not None else 0.0,
         float(ch_off.get("y", 0)) if ch_off is not None else 0.0),
        (float(ch_ext.get("cx", 0)) if ch_ext is not None else 0.0,
         float(ch_ext.get("cy", 0)) if ch_ext is not None else 0.0),
    )


def _sp_box(sp):
    xfrm = sp.find(f"{{{NS['p']}}}spPr/{{{NS['a']}}}xfrm")
    if xfrm is None:
        return None
    off = xfrm.find(f"{{{NS['a']}}}off")
    ext = xfrm.find(f"{{{NS['a']}}}ext")
    if off is None or ext is None:
        return None
    return (float(off.get("x", 0)), float(off.get("y", 0)),
            float(ext.get("cx", 0)), float(ext.get("cy", 0)))


def _sp_text(tx_body) -> tuple[str, bool]:
    """(段落换行拼接的全文, 是否含真实 a:r 文本)。fld-only（页码/日期）不算真实文本。"""
    paras: list[str] = []
    has_run = False
    for p in tx_body.findall(f"{{{NS['a']}}}p"):
        buf: list[str] = []
        for node in p.iter(f"{{{NS['a']}}}r", f"{{{NS['a']}}}fld"):
            t = node.find(f"{{{NS['a']}}}t")
            if t is not None and t.text:
                buf.append(t.text)
                if node.tag == f"{{{NS['a']}}}r":
                    has_run = True
        paras.append("".join(buf))
    return "\n".join(paras), has_run


def _capacity(w_in: float, h_in: float, font_pt: float) -> float:
    line_h = font_pt / 72.0 * 1.25
    char_w = font_pt / 72.0
    lines = (h_in - 0.06) / line_h if line_h > 0 else 0.0
    chars = (w_in - 0.10) / char_w if char_w > 0 else 0.0
    cap = math.floor(max(lines, 0.0)) * math.floor(max(chars, 0.0)) * 0.85
    return min(max(cap, 1.0), 400.0)


def _mark_roles(slots: list[dict], slide_h_in: float) -> None:
    for s in slots:
        sample, w = s["sample"], text_weight(s["sample"])
        if (sample.strip() and any(c.isdigit() for c in sample)
                and _PAGE_NO_RE.match(sample) and w <= 8):
            s["role"] = "page_no"
        elif s["ph_type"] in ("title", "ctrTitle"):
            s["role"] = "title"
        elif s["ph_type"] or s["ph_idx"]:
            # 占位符是明确的填充意图，不走短样本 design 豁免；只按容量降级
            # （raw_cap：design 判定不受样本字重 bump 影响）
            s["role"] = "body" if s["raw_cap"] >= 3 else "design"
        elif (not s["has_run"] or s["raw_cap"] < 3
              or (sample.strip() and (w <= 2 or (w <= 4 and not _HAS_CJK.search(sample))))):
            # 短样本豁免只放行纯装饰位（"·"/"PART 01"/"CONTENTS"），
            # 汉字标签（“团队协作”4 当量）仍是可填内容
            s["role"] = "design"
        elif s["capacity"] <= 10:
            s["role"] = "label"
        elif s["capacity"] <= 45:
            s["role"] = "desc"
        else:
            s["role"] = "body"
    # 无 ph 标题时：顶部 40% 区域内最大字号（≥24pt）唯一者为 title
    if not any(s["role"] == "title" for s in slots):
        cands = [s for s in slots
                 if s["role"] not in ("design", "page_no")
                 and s["font_pt"] >= 24 and s["y"] <= slide_h_in * 0.4]
        if cands:
            best = min(cands, key=lambda s: (-s["font_pt"], s["y"]))
            best["role"] = "title"


def _signature(slide_no: int, layout: str, slots: list[dict]) -> str:
    parts = []
    for s in slots:
        if s["role"] in ("design", "page_no"):
            continue
        parts.append(f"{s['role']}≤{s['capacity']:.0f}")
        if len(parts) >= 10:
            parts.append("…")
            break
    body = " | ".join(parts) if parts else "无填充槽"
    return f"S{slide_no} 布局={layout} [{body}]"


def _role_hint(layout: str, slide_no: int, n_slides: int, fillable: int,
               fillable_cap: float) -> str:
    # 位置优先（英文成品全部挂在 Title Slide 版式上，位置是唯一可靠信号），
    # 版式关键词次之，最后按可填槽数分流。
    if slide_no == 1:
        return "cover"
    if n_slides >= 3 and slide_no == n_slides:
        return "closing"
    L = (layout or "").strip()
    Ll = L.lower()
    # 注意：不做 "Title Slide"→cover 的版式关键词回退——英文成品整副挂在该版式上，
    # 中间页由此被误判 cover；真正的封面/结尾由上面的位置规则决定。
    if "目录" in L or "agenda" in Ll:
        return "toc"
    if "节标题" in L or "section" in Ll:
        return "section"
    if "closing" in Ll or "结尾" in L or "致谢" in L:
        return "closing"
    # 槽少且无大容量正文位（纯标签/构图页）才当表格宿主；
    # title+大 body 占位符页是常规内容页
    if fillable <= 2 and fillable_cap < 60:
        return "table_host"
    return "content"


def _extract_slide(z: zipfile.ZipFile, slide_part: str, slide_no: int,
                   slide_w_in: float, slide_h_in: float) -> tuple[SlideInfo, str]:
    rels = _read_rels(z, slide_part)
    layout_part = next(
        (t for t in rels.values() if t.startswith("ppt/slideLayouts/")), ""
    )
    layout_name = ""
    layout_xml = None
    master_xml = None
    if layout_part:
        layout_xml = z.read(layout_part)
        layout_name = _parse(layout_xml).find(f"{{{NS['p']}}}cSld").get("name", "") or ""
        master_part = next(
            (t for t in _read_rels(z, layout_part).values()
             if t.startswith("ppt/slideMasters/")), ""
        )
        if master_part:
            master_xml = z.read(master_part)

    root = _parse(z.read(slide_part))
    sp_tree = root.find(f"{{{NS['p']}}}cSld/{{{NS['p']}}}spTree")

    slots: list[dict] = []

    def walk(el, ctx: _Ctx, path: list[int]):
        for i, child in enumerate(shape_children(el)):
            p = path + [i]
            if child.tag == _GRP:
                xf = _group_xfrm(child)
                child_ctx = ctx.child_ctx(*xf) if xf else ctx
                walk(child, child_ctx, p)
                continue
            if child.tag != _SP:
                continue
            tx = child.find(f"{{{NS['p']}}}txBody")
            if tx is None:
                continue
            text, has_run = _sp_text(tx)
            ph_type, ph_idx = _ph_key(child)
            if not text.strip() and not (ph_type or ph_idx):
                continue  # 无文本无占位符：装饰或空 shape
            box = _sp_box(child)
            if box is None and (ph_type or ph_idx):
                box = _chain_lookup((layout_xml, master_xml), ph_type, ph_idx, _sp_box_of)
            if box is None:
                continue
            ax, ay, aw, ah = ctx.abs_box(*box)
            font_pt = _explicit_font_pt(tx)
            if font_pt is None and (ph_type or ph_idx):
                font_pt = _chain_lookup((layout_xml, master_xml), ph_type, ph_idx, _sp_font_of)
            if font_pt is None:
                font_pt = _DEFAULT_FONT_PT
            w_in, h_in = aw / _EMU_IN, ah / _EMU_IN
            sample = text.replace("\n", " / ")[:_SAMPLE_MAX]
            raw_cap = _capacity(w_in, h_in, font_pt)
            slots.append({
                "slot_id": ".".join(map(str, p)),
                "path": p,
                "x": ax / _EMU_IN, "y": ay / _EMU_IN, "w": w_in, "h": h_in,
                "font_pt": font_pt,
                "sample": sample,
                # 填充预算取几何公式与模板自身样本字重的较大者——公式对
                # 大字号标题框系统性低估（样本 8 当量 vs 公式 6），照抄公式
                # 会把"感谢观看！"截成"谢谢观"。design 豁免仍看 raw_cap：
                # 英文成品大量单行小框公式容量 1.0 但样本 10+ 字（文字溢出
                # 框外是模板设计意图），按 bump 翻成可填槽会破坏豁免策略
                "capacity": max(raw_cap, text_weight(sample)),
                "raw_cap": raw_cap,
                "ph_type": ph_type, "ph_idx": ph_idx, "has_run": has_run,
            })

    walk(sp_tree, _Ctx(), [])

    _mark_roles(slots, slide_h_in)
    fill_slots = [s for s in slots
                  if s["role"] not in ("design", "page_no") and s["capacity"] >= 3]
    fillable = len(fill_slots)
    max_cap = max((s["capacity"] for s in fill_slots), default=0.0)
    info = SlideInfo(
        slide_no=slide_no,
        layout=layout_name,
        signature=_signature(slide_no, layout_name, slots),
        slots=[SlotInfo(
            slot_id=s["slot_id"], path=s["path"], role=s["role"],
            x=round(s["x"], 3), y=round(s["y"], 3), w=round(s["w"], 3), h=round(s["h"], 3),
            font_pt=s["font_pt"], sample=s["sample"], capacity=s["capacity"],
        ) for s in slots],
        fillable=fillable,
        fillable_cap=round(max_cap, 1),
        role_hint="",
    )
    return info, layout_part


def extract_catalog(pptx_path: Path, display_name: str | None = None) -> TemplateCatalog:
    """确定性提取模板目录。slide_no 取 sldIdLst 顺序（即 PowerPoint 中的页序）。"""
    pptx_path = Path(pptx_path)
    with zipfile.ZipFile(pptx_path) as z:
        pres = _parse(z.read("ppt/presentation.xml"))
        sz = pres.find(f"{{{NS['p']}}}sldSz")
        slide_w_in = float(sz.get("cx", "12192000")) / _EMU_IN
        slide_h_in = float(sz.get("cy", "6858000")) / _EMU_IN
        pres_rels = _read_rels(z, "ppt/presentation.xml")
        ordered: list[str] = []
        for sld_id in pres.find(f"{{{NS['p']}}}sldIdLst"):
            rid = sld_id.get(f"{{{NS['r']}}}id")
            target = pres_rels.get(rid or "")
            if target and target.startswith("ppt/slides/"):
                ordered.append(target)
        slides: list[SlideInfo] = []
        for no, part in enumerate(ordered, start=1):
            info, _ = _extract_slide(z, part, no, slide_w_in, slide_h_in)
            slides.append(info)
    n = len(slides)
    for i, s in enumerate(slides):
        s.role_hint = _role_hint(s.layout, s.slide_no, n, s.fillable, s.fillable_cap)
    return TemplateCatalog(
        stem=pptx_path.stem,
        display_name=display_name or pptx_path.stem,
        n_slides=n,
        slides=slides,
    )


# ---------- 目录读写 ----------

def catalog_path(stem: str, templates_dir: Path = PPTX_TEMPLATES_DIR) -> Path:
    return Path(templates_dir) / f"{stem}.catalog.json"


def template_pptx_path(stem: str, templates_dir: Path = PPTX_TEMPLATES_DIR) -> Path:
    return Path(templates_dir) / f"{stem}.pptx"


def save_catalog(cat: TemplateCatalog, templates_dir: Path = PPTX_TEMPLATES_DIR) -> Path:
    path = catalog_path(cat.stem, templates_dir)
    Path(templates_dir).mkdir(parents=True, exist_ok=True)
    path.write_text(cat.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_catalog(stem: str, templates_dir: Path = PPTX_TEMPLATES_DIR) -> TemplateCatalog:
    path = catalog_path(stem, templates_dir)
    if not path.exists():
        available = ", ".join(s for s, _ in template_choices(templates_dir)) or "（无）"
        raise FileNotFoundError(f"模板 {stem} 不存在（{path}）。可用：{available}")
    cat = TemplateCatalog.model_validate_json(path.read_text(encoding="utf-8"))
    if not template_pptx_path(stem, templates_dir).exists():
        raise FileNotFoundError(f"模板 {stem} 目录存在但缺少 pptx 文件")
    return cat


def list_templates(templates_dir: Path = PPTX_TEMPLATES_DIR) -> list[str]:
    d = Path(templates_dir)
    if not d.exists():
        return []
    suffix = ".catalog.json"
    return sorted(p.name[: -len(suffix)] for p in d.glob(f"*{suffix}"))


def template_choices(templates_dir: Path = PPTX_TEMPLATES_DIR) -> list[tuple[str, str]]:
    """(stem, 显示名) 供 UI 选择器与参数校验。"""
    out: list[tuple[str, str]] = []
    for stem in list_templates(templates_dir):
        display = stem
        try:
            cat = TemplateCatalog.model_validate_json(
                catalog_path(stem, templates_dir).read_text(encoding="utf-8"))
            display = cat.display_name or stem
        except Exception:
            pass
        out.append((stem, display))
    return out
