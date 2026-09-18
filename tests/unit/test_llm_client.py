from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel

from app.llm.client import LLMError, LLMClient
from tests.conftest import FakeAPI


class Ping(BaseModel):
    ok: bool
    message: str


@pytest.fixture
def log_path(tmp_path) -> Path:
    return tmp_path / "llm_calls.jsonl"


def make(script: list, log_path: Path) -> tuple[LLMClient, FakeCompletions]:
    api = FakeAPI(script)
    client = LLMClient(log_path=log_path, api=api)  # type: ignore[arg-type]
    return client, api.chat.completions


def test_chat_ok_and_logged(log_path):
    client, fake = make(["pong"], log_path)
    assert client.chat([{"role": "user", "content": "hi"}], stage="t") == "pong"
    assert len(fake.calls) == 1
    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["ok"] is True and entry["stage"] == "t" and entry["model"]


def test_structured_direct_json(log_path):
    client, fake = make(['{"ok": true, "message": "hi"}'], log_path)
    obj = client.structured(Ping, [{"role": "user", "content": "test"}])
    assert obj.ok is True and obj.message == "hi"
    assert len(fake.calls) == 1


def test_structured_markdown_fence_stripped(log_path):
    client, _ = make(['```json\n{"ok": true, "message": "hi"}\n```'], log_path)
    obj = client.structured(Ping, [{"role": "user", "content": "test"}])
    assert obj.ok is True


def test_structured_json_repair(log_path):
    client, _ = make(["{ok: true, message: 'hi',}"], log_path)
    obj = client.structured(Ping, [{"role": "user", "content": "test"}])
    assert obj.ok is True and obj.message == "hi"


def test_structured_field_level_retry(log_path):
    client, fake = make(
        ['{"ok": "not-a-bool", "message": "x"}', '{"ok": true, "message": "fixed"}'],
        log_path,
    )
    obj = client.structured(Ping, [{"role": "user", "content": "test"}])
    assert obj.ok is True and obj.message == "fixed"
    assert len(fake.calls) == 2
    # 第二次请求应包含校验错误明细
    second = fake.calls[1]["messages"]
    assert any("not-a-bool" in json.dumps(m, ensure_ascii=False) for m in second)


def test_structured_gives_up_after_max_attempts(log_path):
    client, fake = make(["完全不是 JSON 的回复"] * 3, log_path)
    with pytest.raises(LLMError, match="3 次尝试均失败"):
        client.structured(Ping, [{"role": "user", "content": "test"}])
    assert len(fake.calls) == 3


def test_json_mode_downgrade_on_unsupported_response_format(log_path):
    err = APIStatusError(
        "400 response_format unsupported",
        response=types.SimpleNamespace(status_code=400, headers={}, request=None),
        body=None,
    )
    client, fake = make([err, '{"ok": true, "message": "ok"}'], log_path)
    obj = client.structured(Ping, [{"role": "user", "content": "test"}])
    assert obj.ok is True
    assert client.json_mode == "prompt"
    assert "response_format" not in fake.calls[1]


def test_transient_errors_retried(log_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda s: None)
    client, fake = make(
        [APIConnectionError(request=None), APIConnectionError(request=None), "pong"],
        log_path,
    )
    assert client.chat([{"role": "user", "content": "hi"}]) == "pong"
    assert len(fake.calls) == 3
