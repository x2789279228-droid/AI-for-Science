"""LLM Provider 抽象（方案 §3）：OpenAI 兼容 Chat Completions。

支持 DeepSeek / MiniMax / 任意自定义端点；完全不可用时上层走
「抽取式证据模式」降级（方案 §13 风险预案），闭环不中断。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Generator

import httpx

from .. import db
from ..config import settings


class LLMResult(dict):
    @property
    def ok(self) -> bool:
        return bool(self.get("text"))


def _endpoint() -> tuple[str, str, str] | None:
    return settings.llm_endpoint


def chat(messages: list[dict], model: str = "", temperature: float | None = None,
         max_tokens: int = 2048, json_mode: bool = False, module: str = "chat",
         audit_prompt: str = "") -> LLMResult:
    """同步调用。失败返回 {"text":"","error":...}；全程落审计日志。"""
    ep = _endpoint()
    t0 = time.time()
    if not ep:
        db.log_qa(module=module, question=audit_prompt[:200], prompt=audit_prompt,
                  guardrail="llm_unavailable_fallback")
        return LLMResult(text="", error="no_llm_endpoint")
    base_url, api_key, default_model = ep
    model = model or default_model
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": settings.llm_temperature if temperature is None else temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        resp = httpx.post(f"{base_url}/chat/completions",
                          headers={"Authorization": f"Bearer {api_key}"},
                          json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        db.log_qa(module=module, model=model, prompt=audit_prompt,
                  prompt_tokens=usage.get("prompt_tokens", 0),
                  completion_tokens=usage.get("completion_tokens", 0),
                  latency_ms=int((time.time() - t0) * 1000))
        return LLMResult(text=text, model=model,
                         prompt_tokens=usage.get("prompt_tokens", 0),
                         completion_tokens=usage.get("completion_tokens", 0))
    except Exception as e:  # noqa: BLE001 —— 任何 LLM 故障都必须降级而非 500
        db.log_qa(module=module, model=model, prompt=audit_prompt,
                  guardrail=f"llm_error:{type(e).__name__}",
                  latency_ms=int((time.time() - t0) * 1000))
        return LLMResult(text="", error=str(e))


def stream_chat(messages: list[dict], model: str = "", temperature: float | None = None,
                max_tokens: int = 2048) -> Generator[str, None, None]:
    """SSE 流式：逐 token 产出 delta 文本；失败时产出空流（上层降级）。"""
    ep = _endpoint()
    if not ep:
        return
    base_url, api_key, default_model = ep
    payload = {"model": model or default_model, "messages": messages, "stream": True,
               "temperature": settings.llm_temperature if temperature is None else temperature,
               "max_tokens": max_tokens}
    try:
        with httpx.stream("POST", f"{base_url}/chat/completions",
                          headers={"Authorization": f"Bearer {api_key}"},
                          json=payload, timeout=180) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    delta = json.loads(body)["choices"][0].get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        yield piece
                except Exception:
                    continue
    except Exception:
        return


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)

# 连通性探测缓存：key 存在 ≠ key 有效，失效 key 必须真实降级
_ping_cache: dict[str, Any] = {"ts": 0.0, "ok": False}
_PING_TTL = 300.0


def available(force: bool = False) -> bool:
    """LLM 端点是否真正可用（真实 ping，5 分钟缓存）。"""
    if not _endpoint():
        return False
    now = time.time()
    if not force and now - _ping_cache["ts"] < _PING_TTL:
        return _ping_cache["ok"]
    res = chat([{"role": "user", "content": "ping"}], max_tokens=1, module="health_ping")
    _ping_cache["ts"] = now
    _ping_cache["ok"] = bool(res.get("text") or not res.get("error"))
    return _ping_cache["ok"]


def parse_json(text: str) -> dict | None:
    """从 LLM 输出鲁棒提取 JSON 对象。"""
    if not text:
        return None
    m = _JSON_BLOCK.search(text)
    candidate = m.group(1) if m else text
    try:
        return json.loads(candidate)
    except Exception:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(candidate[start:end + 1])
        except Exception:
            return None
    return None
