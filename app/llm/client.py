from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import TypeVar

import json_repair
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

from app.config import DATA_DIR, get_settings

T = TypeVar("T", bound=BaseModel)

_TRANSIENT = (APIConnectionError, APITimeoutError, RateLimitError)


class LLMError(RuntimeError):
    pass


def _hash(messages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


class LLMClient:
    """360智脑（OpenAI 兼容）薄适配层。换模型只改 .env，不动代码。"""

    def __init__(self, log_path: Path | None = None, api: OpenAI | None = None):
        s = get_settings()
        if not s.llm_api_key or not s.llm_base_url or not s.llm_model:
            raise LLMError("LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 未配置（检查 .env）")
        self.client = api or OpenAI(
            base_url=s.llm_base_url, api_key=s.llm_api_key, timeout=s.llm_timeout_s,
            max_retries=0,  # SDK 内置重试会让超时 ×3 且不落日志；重试统一由 chat() 做
        )
        self.model = s.llm_model
        self.vision_model = s.llm_vision_model or s.llm_model
        self.temperature = s.llm_temperature
        self.log_path = Path(log_path) if log_path else DATA_DIR / "llm_calls.jsonl"
        # json_object | prompt（首次失败自动永久降级，进程内缓存）
        self.json_mode: str = "json_object"
        self._log_lock = threading.Lock()

    # ---- 底层调用 ----

    def _create(self, messages: list[dict], response_format: dict | None,
                extra_body: dict | None = None, model: str | None = None):
        kwargs = dict(model=model or self.model, messages=messages,
                      temperature=self.temperature)
        if response_format is not None:
            kwargs["response_format"] = response_format
        if extra_body is not None:
            kwargs["extra_body"] = extra_body
        return self.client.chat.completions.create(**kwargs)

    def chat(self, messages: list[dict], *, stage: str = "chat",
             extra_body: dict | None = None,
             model: str | None = None) -> str:
        response_format = {"type": "json_object"} if self.json_mode == "json_object" and self._mentions_json(messages) else None
        last_err: Exception | None = None
        for attempt in range(1, 4):
            t0 = time.monotonic()
            try:
                resp = self._create(messages, response_format, extra_body, model)
                text = (resp.choices[0].message.content or "").strip()
                self._log(stage, messages, text, time.monotonic() - t0, attempt, ok=True)
                return text
            except _TRANSIENT as e:
                last_err = e
                self._log(stage, messages, "", time.monotonic() - t0, attempt, ok=False, error=f"{type(e).__name__}: {e}")
                time.sleep(min(2 ** attempt, 8))
            except APIStatusError as e:
                # 兼容端点不支持 response_format → 永久降级 prompt 模式后立即重试一次
                if response_format is not None and self.json_mode != "prompt":
                    self.json_mode = "prompt"
                    response_format = None
                    self._log(stage, messages, "", time.monotonic() - t0, attempt, ok=False,
                              error=f"response_format 不被支持，降级 prompt 模式：{e}")
                    continue
                self._log(stage, messages, "", time.monotonic() - t0, attempt, ok=False, error=str(e))
                raise
        raise LLMError(f"chat 连续 3 次瞬时失败：{last_err}")

    @staticmethod
    def _mentions_json(messages: list[dict]) -> bool:
        return "json" in json.dumps(messages, ensure_ascii=False).lower()

    # ---- 结构化输出 ----

    def structured(self, schema: type[T], messages: list[dict], *, stage: str = "structured",
                   max_attempts: int = 3, extra_body: dict | None = None,
                   model: str | None = None) -> T:
        schema_str = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        base = list(messages) + [{
            "role": "system",
            "content": f"你必须只输出一个 JSON 对象（无 markdown 代码块、无解释文字），"
                       f"且严格符合以下 JSON Schema：\n{schema_str}",
        }]
        history = list(base)
        last_detail = "unknown"
        for attempt in range(1, max_attempts + 1):
            raw = self.chat(history, stage=f"{stage}#a{attempt}", extra_body=extra_body,
                            model=model)
            data, err = self._parse(raw)
            if err:
                last_detail = err
            else:
                try:
                    return schema.model_validate(data)
                except ValidationError as e:
                    last_detail = "; ".join(
                        f"{'.'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in e.errors()
                    )
            history = history + [
                {"role": "assistant", "content": raw[:2000]},
                {"role": "user", "content": f"你的输出未通过校验：{last_detail}。"
                                            f"请修正后重新输出完整 JSON（仅 JSON 本体）。"},
            ]
        raise LLMError(f"structured({schema.__name__}) {max_attempts} 次尝试均失败：{last_detail}")

    @staticmethod
    def _parse(raw: str) -> tuple[object | None, str | None]:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")  # 剥 markdown 围栏
            raw = raw[4:] if raw[:4].lower() == "json" else raw
            raw = raw.strip()
        try:
            return json.loads(raw), None
        except json.JSONDecodeError:
            pass
        try:
            repaired = json_repair.loads(raw)
            if isinstance(repaired, (dict, list)):
                return repaired, None
            return None, f"输出无法解析为 JSON：{raw[:120]}"
        except Exception as e:
            return None, f"json_repair 失败：{e}；原文：{raw[:120]}"

    # ---- 调用日志 ----

    def _log(self, stage: str, messages: list[dict], response: str, latency: float,
             attempt: int, *, ok: bool, error: str | None = None) -> None:
        with self._log_lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                entry = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "stage": stage,
                    "model": self.model,
                    "json_mode": self.json_mode,
                    "prompt_hash": _hash(messages),
                    "n_messages": len(messages),
                    # 架构师骨架可达 10KB+，4KB 截断让事后复检（离线重跑
                    # validate/coverage）不可能
                    "response": response[:20000],
                    "latency_s": round(latency, 2),
                    "attempt": attempt,
                    "ok": ok,
                    **({"error": error} if error else {}),
                }
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass  # 日志失败不阻断主流程
