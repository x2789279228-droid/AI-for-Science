"""LLM Provider 抽象（方案 §3）：OpenAI 兼容 Chat Completions。

支持 DeepSeek / MiniMax / 任意自定义端点；完全不可用时上层走
「抽取式证据模式」降级（方案 §13 风险预案），闭环不中断。
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Generator
from typing import Any

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
         audit_prompt: str = "", prompt_version: str = "") -> LLMResult:
    """同步调用。失败返回 {"text":"","error":...}；全程落审计日志（含 prompt 版本号）。"""
    ep = _endpoint()
    t0 = time.time()
    if not ep:
        db.log_qa(module=module, question=audit_prompt[:200], prompt=audit_prompt,
                  guardrail="llm_unavailable_fallback", prompt_version=prompt_version)
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
    if settings.llm_thinking_disabled:
        # 火山方舟 doubao-seed 系列：关闭默认思考模式（其他兼容端点忽略/需容忍该字段）
        payload["thinking"] = {"type": "disabled"}
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
                  latency_ms=int((time.time() - t0) * 1000),
                  prompt_version=prompt_version)
        return LLMResult(text=text, model=model,
                         prompt_tokens=usage.get("prompt_tokens", 0),
                         completion_tokens=usage.get("completion_tokens", 0))
    except Exception as e:  # noqa: BLE001 —— 任何 LLM 故障都必须降级而非 500
        db.log_qa(module=module, model=model, prompt=audit_prompt,
                  guardrail=f"llm_error:{type(e).__name__}",
                  latency_ms=int((time.time() - t0) * 1000),
                  prompt_version=prompt_version)
        return LLMResult(text="", error=str(e))


def chat_json_validated(messages: list[dict], model_cls: Any, *, retries: int = 1,
                        module: str = "chat", audit_prompt: str = "",
                        prompt_version: str = "", **kw) -> dict | None:
    """结构化输出统一入口：json_mode → parse_json → Pydantic 校验 → 失败带错误摘要重试。

    返回校验并矫正后的 dict（model_dump：宽松入参如 ["E1"] → [1]），全部失败返回 None，
    调用方走既有降级通道。修复重试把校验错误摘要注入 follow-up，定向纠错而非盲重试。
    """
    from pydantic import ValidationError

    base = list(messages)
    last_err = ""
    last_text = ""
    for attempt in range(max(1, retries + 1)):
        res = chat(base, json_mode=True, module=module, audit_prompt=audit_prompt,
                   prompt_version=prompt_version, **kw)
        # 兼容 dict / LLMResult 两种返回（测试 stub 常以纯 dict 替身注入）
        text = res.get("text", "") if isinstance(res, dict) else ""
        data = parse_json(text) if text else None
        if data is None:
            last_err = "输出不是可解析的 JSON" if text else f"LLM 调用失败: {res.get('error', '')}"
            last_text = text
        else:
            try:
                # model_dump() 返回矫正后的数据（如 evidence_ids ["E1"]→[1]），而非原始输入
                return model_cls.model_validate(data).model_dump()
            except ValidationError as e:
                errs = "; ".join(
                    ".".join(str(p) for p in err["loc"]) + f": {err['msg']}"
                    for err in e.errors()[:3])
                last_err = f"JSON 未通过 schema 校验：{errs}"
                last_text = text
        if attempt < retries:
            base = base + [
                {"role": "assistant", "content": (last_text or "")[:2000]},
                {"role": "user",
                 "content": f"你的 JSON 输出不合法：{last_err}。"
                            "请只输出修正后的合法 JSON，不要解释、不要输出 Markdown 代码块。"},
            ]
    return None


def stream_chat(messages: list[dict], model: str = "", temperature: float | None = None,
                max_tokens: int = 2048, module: str = "", audit_prompt: str = "",
                prompt_version: str = "") -> Generator[str, None, None]:
    """SSE 流式：逐 token 产出 delta 文本；失败时产出空流（上层降级）。

    传入 module 时在流结束（finally）落一条 qa_logs 审计（guardrail=stream），
    让主问答路径的 prompt 版本号也可审计——此前流式路径不落审计，是审计盲区。
    """
    ep = _endpoint()
    if not ep:
        return
    base_url, api_key, default_model = ep
    model = model or default_model
    payload = {"model": model, "messages": messages, "stream": True,
               "temperature": settings.llm_temperature if temperature is None else temperature,
               "max_tokens": max_tokens,
               "stream_options": {"include_usage": True}}   # 末块带 usage；不支持的端点忽略
    if settings.llm_thinking_disabled:
        payload["thinking"] = {"type": "disabled"}
    t0 = time.time()
    parts: list[str] = []
    usage: dict = {}
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
                    parsed = json.loads(body)
                    if isinstance(parsed.get("usage"), dict):   # usage 块（include_usage 末块）
                        usage = parsed["usage"]
                    delta = parsed["choices"][0].get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        parts.append(piece)
                        yield piece
                except Exception:  # noqa: BLE001, S112 —— 单块解析失败跳过，流不中断
                    continue
    except Exception:  # noqa: BLE001, S110 —— 流式失败返回空，调用方走非流式降级
        pass
    finally:
        if module:
            try:
                db.log_qa(module=module, model=model, question=audit_prompt[:200],
                          prompt="[stream]", answer="".join(parts)[:8000],
                          prompt_tokens=int(usage.get("prompt_tokens") or 0),
                          completion_tokens=int(usage.get("completion_tokens") or 0),
                          latency_ms=int((time.time() - t0) * 1000),
                          guardrail="stream", prompt_version=prompt_version)
            except Exception:  # noqa: BLE001, S110 —— 审计失败不影响已产出的流
                pass


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)

# 连通性探测缓存：key 存在 ≠ key 有效，失效 key 必须真实降级
# P0-7：成功缓存 300s；失败只缓存 30s（瞬时网络抖动不应把可用服务锁死 5 分钟），且失败前重试
_ping_cache: dict[str, Any] = {"ts": 0.0, "ok": False}
_PING_TTL = 300.0
_PING_TTL_FAIL = 30.0
_PING_RETRIES = 2


def available(force: bool = False) -> bool:
    """LLM 端点是否真正可用（真实 ping；成功 TTL 300s / 失败 TTL 30s + 探测前重试）。"""
    if not _endpoint():
        return False
    now = time.time()
    ttl = _PING_TTL if _ping_cache["ok"] else _PING_TTL_FAIL
    if not force and now - _ping_cache["ts"] < ttl:
        return _ping_cache["ok"]
    ok = False
    for attempt in range(_PING_RETRIES + 1):
        res = chat([{"role": "user", "content": "ping"}], max_tokens=1, module="health_ping")
        ok = bool(res.get("text") or not res.get("error"))
        if ok:
            break
        if attempt < _PING_RETRIES:
            time.sleep(0.5)
    _ping_cache["ts"] = time.time()
    _ping_cache["ok"] = ok
    return ok


def parse_json(text: str) -> dict | None:
    """从 LLM 输出鲁棒提取 JSON 对象。"""
    if not text:
        return None
    m = _JSON_BLOCK.search(text)
    candidate = m.group(1) if m else text
    try:
        return json.loads(candidate)
    except Exception:  # noqa: BLE001, S110 —— 非 JSON 文本进入截取提取
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(candidate[start:end + 1])
        except Exception:  # noqa: BLE001 —— 截取仍非 JSON → 调用方走降级
            return None
    return None
