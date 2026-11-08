"""Prompt YAML 加载器：版本化 + 运行时 A/B 开关（PROMPT_VARIANT）+ 内置常量保底。

- 每个 `app/prompts/{module}.yaml` 含 version + changelog + variants{default, strict}
- `get_prompt(module, slot)` 运行时读取 `settings.prompt_variant`（改 env 即 A/B，无需重启语义层）
- YAML 缺失/非法/槽位缺失 → 返回 None，调用方回退内置常量（`resolve` 一站式封装）
- 版本号随调用写入 qa_logs.prompt_version → /api/audit/prompts 可见版本分布
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import settings
from ..log import logger

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

_lock = threading.Lock()
_cache: dict[str, dict] = {}     # module -> 解析后的 YAML（失败记 {}）
_loaded_ok: set[str] = set()     # 成功解析过的模块（只警告一次）


@dataclass(frozen=True)
class Prompt:
    text: str
    version: str
    variant: str


def _load(module: str) -> dict:
    """读取并缓存模块 YAML；缺失/非法返回 {}（不抛异常，走内置兜底）。"""
    with _lock:
        if module in _cache:
            return _cache[module]
        path = PROMPT_DIR / f"{module}.yaml"
        data: dict = {}
        if path.exists():
            try:
                parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
                variants = (parsed or {}).get("variants")
                if not isinstance(parsed, dict) or not isinstance(variants, dict) \
                        or not isinstance(variants.get("default"), dict):
                    raise ValueError("结构须为 {version, changelog, variants.default{slot}}")  # noqa: TRY004
                data = parsed
                _loaded_ok.add(module)
            except Exception as e:  # noqa: BLE001 —— prompt 文件损坏不得阻塞问答主链路
                logger.warning(f"prompt/{module}.yaml 解析失败 → 内置常量兜底: "
                               f"{type(e).__name__}: {e}", module="prompts")
        _cache[module] = data
        return data


def get_prompt(module: str, slot: str, *, variant: str | None = None) -> Prompt | None:
    """运行时取 prompt；variant 缺省用 settings.prompt_variant，档位缺槽回退 default。"""
    data = _load(module)
    if not data:
        return None
    variants = data.get("variants") or {}
    want = variant or (settings.prompt_variant or "default")
    body = variants.get(want)
    used = want
    if not isinstance(body, dict) or not body.get(slot):
        body, used = variants.get("default") or {}, "default"
    text = body.get(slot)
    if not isinstance(text, str) or not text.strip():
        return None
    return Prompt(text=text.strip(), version=str(data.get("version", "0")), variant=used)


def resolve(module: str, slot: str, builtin: str) -> tuple[str, str]:
    """(text, version) 一站式：YAML 优先，缺失回退内置常量（version='builtin'）。"""
    p = get_prompt(module, slot)
    return (p.text, p.version) if p else (builtin, "builtin")


def prompt_version(module: str, slot: str) -> str:
    """只要版本号（审计用），兜底 'builtin'。"""
    p = get_prompt(module, slot)
    return p.version if p else "builtin"


def reload() -> None:
    """清缓存（热更新场景/测试用）。"""
    with _lock:
        _cache.clear()
        _loaded_ok.clear()


def catalog() -> list[dict]:
    """审计用清单：每个模块的 version/changelog/slots。"""
    out = []
    for path in sorted(PROMPT_DIR.glob("*.yaml")):
        data = _load(path.stem)
        if data:
            slots = {v: sorted(body.keys()) for v, body in
                     (data.get("variants") or {}).items() if isinstance(body, dict)}
            out.append({"module": path.stem, "version": str(data.get("version", "0")),
                        "changelog": data.get("changelog") or [], "slots": slots})
    return out
