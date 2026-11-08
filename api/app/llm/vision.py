"""VLM 视觉通道（扫描件/困难页兜底，评分表口径"只对失败页调用"）。

端点解析顺序：显式 vlm_base_url+vlm_api_key → DashScope（qwen-vl-max）→
跟随 LLM 端点（火山方舟 token plan：doubao-seed 系列本身多模态，实测整页识读准确）。
httpx 直调无需 SDK；失败返回 None 不抛异常，全量落审计日志。
"""
from __future__ import annotations

import base64
import time

import httpx

from .. import db
from ..config import settings
from ..log import logger

_VLM_AVAILABLE: bool | None = None
_VLM_CHECKED_AT: float = 0.0
_CHECK_TTL = 300  # seconds

_DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DASHSCOPE_MODEL = "qwen-vl-max"


def _endpoint() -> tuple[str, str, str] | None:
    """返回 (base_url, api_key, model)；未配置返回 None。"""
    if settings.vlm_base_url and settings.vlm_api_key:
        return settings.vlm_base_url.rstrip("/"), settings.vlm_api_key, \
            settings.vlm_model or "qwen-vl-max"
    if settings.dashscope_api_key:
        return _DASHSCOPE_BASE, settings.dashscope_api_key, \
            settings.vlm_model or _DASHSCOPE_MODEL
    ep = settings.llm_endpoint   # 跟随 LLM：方舟 token plan 的 doubao-seed 系列多模态
    if ep:
        return ep[0], ep[1], settings.vlm_model or ep[2]
    return None


def available(force: bool = False) -> bool:
    """VLM 是否可用（有 key 即认为可用；真实连通性在调用时失败静默降级）。"""
    global _VLM_AVAILABLE, _VLM_CHECKED_AT
    if not settings.vlm_enabled:
        return False
    ep = _endpoint()
    if not ep:
        return False
    now = time.time()
    if _VLM_AVAILABLE is None or force or (now - _VLM_CHECKED_AT) > _CHECK_TTL:
        _VLM_AVAILABLE = True  # 仅探测配置存在性；网络故障在调用层兜底
        _VLM_CHECKED_AT = now
    return _VLM_AVAILABLE


def image_data_url(png_bytes: bytes) -> str:
    """PNG 字节 → data URL（OpenAI 兼容 image_url 形状）。"""
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode()


def _audit(module: str, model: str, question: str, answer: str, latency_ms: int) -> None:
    """审计日志尽力而为：qa_logs 未建表/落库失败不影响解析主链路。"""
    try:
        db.log_qa(module=module, model=model, question=question[:400],
                  prompt="[vlm]", answer=answer[:8000], latency_ms=latency_ms)
    except Exception:  # noqa: BLE001, S110
        pass


def vlm_chat(prompt: str, images_png: list[bytes], *,
             temperature: float = 0.1, max_tokens: int = 3000,
             module: str = "vlm", audit_prompt: str = "") -> str | None:
    """视觉问答：页面图 → 结构化 Markdown/JSON。失败返回 None（调用方降级）。"""
    ep = _endpoint()
    if not ep:
        return None
    base_url, api_key, model = ep
    content: list[dict] = [{"type": "image_url",
                            "image_url": {"url": image_data_url(img)}} for img in images_png]
    content.append({"type": "text", "text": prompt})
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是专业的学术文档版面识别引擎，只输出要求的内容，禁止编造。"},
            {"role": "user", "content": content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    t0 = time.time()
    text = ""
    err = ""
    for attempt in range(2):   # 方舟偶发 429/5xx：一次退避重试
        try:
            resp = httpx.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload, timeout=180,
            )
            resp.raise_for_status()
            text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
            err = ""
            break
        except Exception as e:  # noqa: BLE001 —— VLM 属兜底通道，失败静默降级
            err = f"{type(e).__name__}: {e}"[:200]
            if attempt == 0:
                time.sleep(3)
    if err or not text:
        if err:
            logger.warning(f"vlm 调用失败 → 降级: {err}", module="vlm")
        _audit(module, model, audit_prompt, f"error: {err or 'empty response'}",
               int((time.time() - t0) * 1000))
        return None
    _audit(module, model, audit_prompt, text, int((time.time() - t0) * 1000))
    return text
