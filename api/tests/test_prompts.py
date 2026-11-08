"""Prompt 版本化 + Guardrails 测试（4.5，密闭）。

覆盖：YAML 目录完整性 / loader 回退 / PROMPT_VARIANT 运行时 A/B / 反注入与伦理条款 /
chat_json_validated 修复重试 / evidence_ids 宽松矫正 / prompt_version 审计落库。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR", str(Path(__file__).parent / f"_pmp_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_PROVIDER"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["MINIMAX_API_KEY"] = ""     # 本机全局 env 有 minimax key，必须显式清空（密闭性）
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["MINERU_ENABLED"] = "off"
os.environ["EMBEDDING_BACKEND"] = "off"

from app import db
from app.api_routes import routes
from app.config import settings
from app.llm import client as llm
from app.llm import prompts
from app.llm.schemas import ClaimList, QuoteMap
from app.rag import answering, verify

DATA = Path(os.environ["SCHOLAR_DATA_DIR"])


@pytest.fixture(autouse=True)
def _fresh_loader(monkeypatch):
    """每用例独立 loader 缓存 + default 档位，避免跨用例污染。"""
    monkeypatch.setattr(settings, "prompt_variant", "default")
    prompts.reload()
    yield
    prompts.reload()


# ---------- YAML 目录与 loader ----------

def test_yaml_catalog_complete():
    catalog = {c["module"]: c for c in prompts.catalog()}
    for m in ("qa", "compare", "writer", "graph", "claims", "judge"):
        assert m in catalog, f"缺少 app/prompts/{m}.yaml"
        assert catalog[m]["version"] == "2.1.0"
        assert isinstance(catalog[m]["changelog"], list) and catalog[m]["changelog"]
        assert "default" in catalog[m]["slots"]


def test_loader_resolves_and_falls_back():
    p = prompts.get_prompt("qa", "system_qa")
    assert p is not None
    assert "科研文献问答助手" in p.text
    assert p.version == "2.1.0"
    # 槽位缺失 → 内置兜底
    text, ver = prompts.resolve("qa", "nope_slot", "BUILTIN")
    assert text == "BUILTIN" and ver == "builtin"
    # 模块缺失 → 内置兜底
    text2, ver2 = prompts.resolve("no_such_module", "system_qa", "BUILTIN2")
    assert text2 == "BUILTIN2" and ver2 == "builtin"


def test_variant_ab_runtime(monkeypatch):
    default = prompts.get_prompt("qa", "system_qa", variant="default")
    strict = prompts.get_prompt("qa", "system_qa", variant="strict")
    assert default is not None and strict is not None
    assert default.text != strict.text
    assert "300 字" in strict.text          # strict 档特征：更短输出上限
    # 运行时开关（settings 即 env PROMPT_VARIANT 的运行时视图）
    monkeypatch.setattr(settings, "prompt_variant", "strict")
    p = prompts.get_prompt("qa", "system_qa")
    assert p is not None and p.variant == "strict"


# ---------- Guardrails 条款 ----------

def test_anti_injection_clause_in_all_data_bound_prompts():
    for mod, slot in [("qa", "system_qa"), ("compare", "system_compare"),
                      ("writer", "system_writer"), ("graph", "system_graph"),
                      ("claims", "system_claims"), ("judge", "system_judge")]:
        p = prompts.get_prompt(mod, slot)
        assert p is not None and "数据而非指令" in p.text, f"{mod}.{slot} 缺反注入条款"


def test_writer_ethics_clause():
    p = prompts.get_prompt("writer", "system_writer")
    assert p is not None
    assert "学术伦理" in p.text and "辅助草稿" in p.text


def test_build_qa_messages_yaml_and_language(monkeypatch):
    msgs = answering.build_qa_messages("问题", [], language="zh")
    assert "安全边界" in msgs[0]["content"]            # 条款来自 YAML（内置常量无此条）
    assert "语言边界：使用 zh 回答" in msgs[0]["content"]
    monkeypatch.setattr(settings, "prompt_variant", "strict")
    msgs2 = answering.build_qa_messages("问题", [])
    assert "300 字" in msgs2[0]["content"]             # 运行时 A/B 生效


def test_refusal_constants_keep_placeholders():
    assert "Meridian" in answering.REFUSAL_CHITCHAT
    assert "未找到依据" in answering.NO_EVIDENCE
    assert "{n}" in answering.COMPARE_REFUSAL
    assert "{n}" in answering.WRITER_REFUSAL and "{m}" in answering.WRITER_REFUSAL


# ---------- chat_json_validated ----------

class _SeqStub:
    """按序返回预设 LLMResult，记录每次调用的 msgs/kw。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, msgs, **kw):
        self.calls.append({"msgs": msgs, "kw": kw})
        return self.results.pop(0)


def test_chat_json_validated_repairs_once(monkeypatch):
    bad = llm.LLMResult(text='{"claims":[{"text":"","evidence_ids":[1]}]}')   # text 空 → 校验失败
    good = llm.LLMResult(text='{"claims":[{"text":"x","evidence_ids":[1,2]}]}')
    stub = _SeqStub([bad, good])
    monkeypatch.setattr(llm, "chat", stub)
    data = llm.chat_json_validated([{"role": "user", "content": "go"}], ClaimList,
                                   module="claims", prompt_version="2.1.0")
    assert data is not None and data["claims"][0]["evidence_ids"] == [1, 2]
    assert len(stub.calls) == 2                                   # 坏 → 修复重试恰好 1 次
    repair = stub.calls[1]["msgs"][-1]["content"]
    assert "不合法" in repair                                     # 校验错误摘要注入 follow-up
    assert stub.calls[0]["kw"].get("prompt_version") == "2.1.0"


def test_chat_json_validated_always_bad_returns_none(monkeypatch):
    stub = _SeqStub([llm.LLMResult(text="not json"), llm.LLMResult(text="still not json")])
    monkeypatch.setattr(llm, "chat", stub)
    assert llm.chat_json_validated([{"role": "user", "content": "go"}], ClaimList,
                                   retries=1) is None
    assert len(stub.calls) == 2


def test_chat_json_validated_no_extra_call_when_valid(monkeypatch):
    stub = _SeqStub([llm.LLMResult(text='{"quotes":{"a":"b"}}')])
    monkeypatch.setattr(llm, "chat", stub)
    data = llm.chat_json_validated([{"role": "user", "content": "go"}], QuoteMap)
    assert data == {"quotes": {"a": "b"}}
    assert len(stub.calls) == 1


def test_llm_claims_lenient_string_eids(monkeypatch):
    """LLM 把 evidence_ids 输出成 "E1" 字符串 → schema 矫正 → claims 不再全军覆没（实测教训）。"""
    hits = [{"chunk_id": "c1", "paper_id": "p1", "page": 2, "section_path": "§1",
             "text": "The model was trained on 8 NVIDIA P100 GPUs.", "score": 1.0}]
    ans = "训练用了 8 个 P100 GPU。[E1]"
    payload = json.dumps({"claims": [{"text": ans, "evidence_ids": ["E1"],
                                      "quote": "trained on 8 NVIDIA P100 GPUs"}]},
                         ensure_ascii=False)
    monkeypatch.setattr(llm, "chat", _SeqStub([llm.LLMResult(text=payload)]))
    claims, eid_map = answering.llm_claims(ans, hits)
    assert len(claims) == 1 and claims[0]["status"] == "supported"
    assert claims[0]["support"][0]["eid"] == "E1" and eid_map


# ---------- judge / reextract 走校验通道 ----------

def test_judge_no_verdict_unsupports(monkeypatch):
    claims = [
        {"text": "A", "status": "supported", "support": [{"eid": "E1"}]},
        {"text": "B", "status": "needs_check", "support": [{"eid": "E1"}],
         "unverified_numbers": ["8"]},
    ]
    monkeypatch.setattr(llm, "available", lambda force=False: True)
    payload = json.dumps({"verdicts": [{"id": 0, "verdict": "yes"},
                                       {"id": 1, "verdict": "no"}]})
    monkeypatch.setattr(llm, "chat", _SeqStub([llm.LLMResult(text=payload)]))
    out, guards = verify.judge_claims(claims, {})
    assert out[0]["status"] == "supported"
    assert out[1]["status"] == "unsupported"
    assert "unverified_numbers" not in out[1]      # no 判定覆盖数值黄标
    assert guards == []


def test_judge_payload_malformed_skips(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda force=False: True)
    monkeypatch.setattr(llm, "chat", _SeqStub([llm.LLMResult(text='{"verdicts":"oops"}'),
                                               llm.LLMResult(text='{"verdicts":"oops"}')]))
    claims = [{"text": "A", "status": "supported", "support": [{"eid": "E1"}]}]
    out, guards = verify.judge_claims(claims, {})
    assert out[0]["status"] == "supported" and guards == ["semantic_judge_skipped"]


# ---------- Prompt 审计（qa_logs.prompt_version）----------

def test_qa_logs_prompt_version_and_audit_route():
    db.init_db()
    db.log_qa(module="t", prompt="p", prompt_version="2.1.0")
    db.log_qa(module="t2", prompt="p")   # 旧式调用无版本 → 空串 → legacy
    row = db.query("SELECT prompt_version FROM qa_logs WHERE module='t'")[0]
    assert row["prompt_version"] == "2.1.0"
    data = routes.audit_prompts(limit=10)
    versions = {v["version"]: v["n"] for v in data["prompt_versions"]}
    assert versions.get("2.1.0", 0) >= 1
    assert "legacy" in versions
    assert len(data["prompt_catalog"]) >= 6
    assert data["prompt_variant"] == "default"
    assert data["prompt_catalog"][0]["module"]           # catalog 含模块名与版本
    assert all("version" in c for c in data["prompt_catalog"])


def test_old_db_migration_adds_prompt_version():
    """旧库（无 prompt_version 列）→ init_db 迁移补列，数据不丢。"""
    cols = [r[1] for r in db.connect().execute("PRAGMA table_info(qa_logs)")]
    assert "prompt_version" in cols
