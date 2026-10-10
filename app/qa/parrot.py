"""模板样本回填检测：填充文本与模板槽示例文案的最长公共子串汉字当量。

槽被"填过"故 E_LEFTOVER 不报，ngram 只对比源文档——照抄模板示例文案是
两道防线的公共盲区（N 轮验收实弹：indie 目录文案整段进入腰椎 PPT p3）。
"""
from __future__ import annotations

from difflib import SequenceMatcher

from app.schema.textlen import text_weight

PARROT_WEIGHT = 6.0


def parrot_weight(text: str, sample: str) -> float:
    """填充文本与模板样本的最长公共子串当量；≥ PARROT_WEIGHT 视为照抄。

    阈值 6：放过合法短回声（"目录"/"谢谢观看" ≤4 当量、短拉丁装饰词
    ≤4.5），拦住真实文案（"WHAT IS A ZINE" ≈7、"ZINE 是什么 + 简史四浪潮"
    ≈12）。
    """
    if not text.strip() or not sample.strip():
        return 0.0
    m = SequenceMatcher(None, sample, text, autojunk=False).find_longest_match(
        0, len(sample), 0, len(text))
    return text_weight(sample[m.a:m.a + m.size])
