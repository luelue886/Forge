from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config import SKINS_DIR

_HEX = re.compile(r"^[0-9A-Fa-f]{6}$")


@dataclass(frozen=True)
class Skin:
    name: str
    font: str
    bg: str
    text: str
    title: str
    accent: str
    muted: str
    cover_bg: str
    cover_title: str
    cover_subtitle: str
    section_bg: str
    header_bg: str
    header_text: str
    zebra: str
    card_bg: str
    card_border: str
    footer_text: str
    divider: str
    column_heading: str


_FIELDS = [f for f in Skin.__dataclass_fields__ if f not in ("name", "font")]


def load_skin(name: str, skins_dir: Path = SKINS_DIR) -> Skin:
    path = skins_dir / f"{name}.yaml"
    if not path.exists():
        available = ", ".join(list_skins(skins_dir)) or "（无）"
        raise FileNotFoundError(f"皮肤 {name} 不存在（{path}）。可用：{available}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = {k: v for k, v in data.items() if k in Skin.__dataclass_fields__}
    data["name"] = name
    # YAML 会把 232333 这类纯数字解析成 int；str() 可无损还原（八进制吞前导零的情况会在校验报错）
    for f in _FIELDS:
        data[f] = str(data[f]).upper() if f in data and data[f] is not None else ""
    missing = [f for f in _FIELDS if not _HEX.match(data.get(f, ""))]
    if missing:
        raise ValueError(f"皮肤 {name} 缺失或非法颜色字段：{missing}")
    return Skin(**data)


def list_skins(skins_dir: Path = SKINS_DIR) -> list[str]:
    if not skins_dir.exists():
        return []
    return sorted(p.stem for p in skins_dir.glob("*.yaml"))


def skin_choices(skins_dir: Path = SKINS_DIR) -> list[tuple[str, str]]:
    """(stem, 显示名) 供 UI 选择器；显示名取 YAML name 字段，缺省回退 stem。"""
    out: list[tuple[str, str]] = []
    for stem in list_skins(skins_dir):
        display = stem
        try:
            data = yaml.safe_load((skins_dir / f"{stem}.yaml").read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("name"):
                display = str(data["name"])
        except Exception:
            pass
        out.append((stem, display))
    return out
