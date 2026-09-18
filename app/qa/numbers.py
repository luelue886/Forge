"""数字溯源 QA：生成文本中的数字必须能在源文档找到同值出处。

表格单元格 verbatim 照搬天然可溯源；本检查把住"正文里的数字不得编造"。
"""

from __future__ import annotations

import re

_NUM_TOKEN = re.compile(r"[0-9０-９]+(?:[.,，．][0-9０-９]+)*")
_FULLWIDTH = str.maketrans("０１２３４５６７８９，．", "0123456789,.")

# ≤99 的纯整数计数豁免溯源：源文"三"/生成"3"这类写法转换合法，无法逐值对账
COUNT_WHITELIST_MAX = 99


def _normalize(token: str) -> str:
    t = token.translate(_FULLWIDTH)
    t = t.replace(",", "").replace("，", "")
    return t.rstrip(".").rstrip("．")


def extract_number_tokens(text: str) -> list[str]:
    return [t for t in (_normalize(m.group()) for m in _NUM_TOKEN.finditer(text)) if t]


def check_numbers(generated: str, source: str) -> list[tuple[str, str]]:
    """返回 [(数字, 上下文)]：生成文本出现、源文档未出现的数字（白名单外）。"""
    src_tokens = set(extract_number_tokens(source))
    out: list[tuple[str, str]] = []
    for m in _NUM_TOKEN.finditer(generated):
        tok = _normalize(m.group())
        if not tok or tok in src_tokens:
            continue
        if tok.isdigit() and int(tok) <= COUNT_WHITELIST_MAX:
            continue
        start = max(0, m.start() - 10)
        ctx = generated[start:m.end() + 10].replace("\n", " ")
        out.append((tok, f"…{ctx}…"))
    return out
