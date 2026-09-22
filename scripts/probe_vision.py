"""视觉模型探针：逐候选发一张含文字的小图，找第一个能读图的模型。

用法：.venv/Scripts/python.exe -X utf8 scripts/probe_vision.py [候选名…]
不传候选则用内置清单（第一个是 LLM_MODEL 本身——可能已支持多模态）。
找到后打印建议配置行（写进 .env 的 LLM_VISION_MODEL，由用户自行修改）。
"""

from __future__ import annotations

import base64
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

from app.config import get_settings

CANDIDATES = [
    None,  # LLM_MODEL 本身
    "z-ai/glm-4.5v",
    "glm-4.5v",
    "glm-4v-flash",
    "qwen-vl-plus",
    "gpt-4o-mini",
]


def make_img_b64() -> str:
    img = Image.new("RGB", (360, 120), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(get_settings().font_path, 32)
    d.text((20, 40), "安全生产费用 5%", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def main() -> int:
    s = get_settings()
    if not s.llm_api_key or not s.llm_base_url:
        print("[fail] .env 未配置 LLM_API_KEY / LLM_BASE_URL")
        return 1
    extra = [a for a in sys.argv[1:] if not a.startswith("-")]
    candidates = extra or CANDIDATES
    api = OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key,
                 timeout=90, max_retries=0)
    b64 = make_img_b64()
    img_block = {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
    winner = None
    for cand in candidates:
        model = cand or s.llm_model
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "这张图片里有哪些文字？只输出图中文字本体。"},
            img_block,
        ]}]
        t0 = time.monotonic()
        try:
            r = api.chat.completions.create(model=model, messages=msgs,
                                            temperature=0)
            text = (r.choices[0].message.content or "").strip()
            dt = time.monotonic() - t0
            ok = "安全生产" in text
            print(f"[{model}] {dt:6.1f}s  {'OK ' if ok else 'IGN'}  reply={text[:60]!r}",
                  flush=True)
            if ok:
                winner = model
                break
        except Exception as e:  # noqa: BLE001 — 探针要逐候选报错继续
            print(f"[{model}] FAIL {type(e).__name__}: {str(e)[:110]}",
                  flush=True)
    if winner is None:
        print("\n所有候选均失败。请提供确切的视觉模型名（写进 .env 的 "
              "LLM_VISION_MODEL）。")
        return 1

    # JSON 结构化 sanity：抽检走 structured()，需确认视觉模型能出 JSON
    json_msgs = [{"role": "user", "content": [
        {"type": "text", "text": '读取图片文字，只输出 JSON：{"text":"<图中文字>"}'},
        img_block,
    ]}]
    for label, kwargs in (("json_object", {"response_format": {"type": "json_object"}}),
                          ("prompt", {})):
        try:
            r = api.chat.completions.create(model=winner, messages=json_msgs,
                                            temperature=0, **kwargs)
            text = (r.choices[0].message.content or "").strip()
            print(f"[json/{label}] reply={text[:80]!r}", flush=True)
            if "安全生产" in text:
                break
        except Exception as e:  # noqa: BLE001
            print(f"[json/{label}] FAIL {type(e).__name__}: {str(e)[:110]}",
                  flush=True)
    print(f"\n可用视觉模型：{winner}")
    if winner != s.llm_model:
        print(f"请在 .env 中加一行：LLM_VISION_MODEL={winner}")
    else:
        print("LLM_MODEL 本身即支持图片输入，LLM_VISION_MODEL 可留空。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
