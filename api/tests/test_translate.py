"""翻译服务测试（4.7，密闭）：占位符保护/回退、术语表、缓存续译、划词、SSE 端点。"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR", str(Path(__file__).parent / f"_trl_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_PROVIDER"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["MINIMAX_API_KEY"] = ""
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["MINERU_ENABLED"] = "off"
os.environ["EMBEDDING_BACKEND"] = "off"

from app import db, translate
from app.config import DOCIR_DIR
from app.llm import client as llm
from app.llm import prompts

DATA = Path(os.environ["SCHOLAR_DATA_DIR"])
PID = "p_trl"

_DOCIR = {
    "meta": {"title": "T"},
    "blocks": [
        {"id": "b1", "type": "paragraph", "text": "The encoder uses $\\alpha$ attention [12] and `multi-head` layers.",
         "page": 0, "section_path": "1 Introduction"},
        {"id": "b2", "type": "paragraph", "text": "Training took 3.5 days on 8 GPUs.",
         "page": 0, "section_path": "2 Method"},
        {"id": "b3", "type": "paragraph", "text": "Future section text.", "page": 1, "section_path": "3 Results"},
        {"id": "f1", "type": "formula", "text": "E = mc^2", "latex": "E = mc^2", "page": 1, "section_path": "3 Results"},
    ],
}


@pytest.fixture(scope="module", autouse=True)
def _setup():
    db.init_db()
    Path(DOCIR_DIR).mkdir(parents=True, exist_ok=True)
    (Path(DOCIR_DIR) / f"{PID}.json").write_text(json.dumps(_DOCIR), encoding="utf-8")
    prompts.reload()
    yield


class _Stub:
    """按调用序返回（单请求场景用）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, msgs, **kw):
        with self.lock:
            self.calls += 1
        return {"text": self.replies[min(self.calls - 1, len(self.replies) - 1)], "model": "stub"}


class _ContentStub:
    """按请求内容匹配回复（并发场景必须：两 worker 抢占调用序会串台）。"""

    def __init__(self, mapping):
        self.mapping = list(mapping)          # [(内容子串, 回复), ...]
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, msgs, **kw):
        content = msgs[-1]["content"]
        with self.lock:
            self.calls += 1
        for key, reply in self.mapping:
            if key in content:
                return {"text": reply, "model": "stub"}
        return {"text": "", "error": "stub_no_match"}


# ---------- 占位符保护 ----------

def test_protect_and_restore_roundtrip():
    text = "Uses $\\alpha$ attention [12] and `multi-head` layers."
    protected, mapping = translate.protect(text)
    assert "⟦" in protected and "$" not in protected and "[12]" not in protected
    restored, ok = translate.restore(protected, mapping)
    assert ok and restored == text


def test_restore_missing_placeholder_fails():
    protected, mapping = translate.protect("a $x$ b [3] c")
    translated = protected.replace("⟦2⟧", "")          # 模拟 LLM 吃掉一个占位符
    restored, ok = translate.restore(translated, mapping)
    assert not ok and restored == ""


def test_terminology_protect_maps_to_translation():
    db.upsert_terminology("Transformer", "变换器")
    protected, mapping = translate.protect("The Transformer model works.")
    assert "Transformer" not in protected
    restored, ok = translate.restore(protected, mapping)
    assert ok and "变换器" in restored and "Transformer" not in restored
    assert translate.hit_terms("the transformer is great")[0]["term"] == "Transformer"


def test_select_block_ids_filters():
    ids_page = translate.select_block_ids(_DOCIR, page=0)
    assert ids_page == ["b1", "b2"]
    ids_sec = translate.select_block_ids(_DOCIR, section="3 ")
    assert ids_sec == ["b3"]
    all_ids = translate.select_block_ids(_DOCIR)
    assert "f1" not in all_ids                          # 公式块不翻译


# ---------- 批量翻译：缓存 / 回退 / 限流 ----------

@pytest.fixture()
def _fresh_translations():
    """清空译文缓存，隔离各用例的缓存状态。"""
    with db.tx() as c:
        c.execute("DELETE FROM translations")
    yield


def test_translate_blocks_cache_and_rerun(monkeypatch, _fresh_translations):
    # LLM 返回：保留全部占位符的中文译文（b1 有 $α$、[12]、`multi-head` 三个占位符）
    stub = _ContentStub([("encoder", "编码器使用 ⟦1⟧ 注意力 ⟦2⟧ 和 ⟦3⟧ 层。"),
                         ("GPUs", "训练在 8 个 GPU 上耗时 3.5 天。")])
    monkeypatch.setattr(llm, "chat", stub)

    stats = translate.translate_blocks(PID, ["b1", "b2"], progress=lambda *a: None)
    assert stats["done"] == 2 and stats["failed"] == 0 and stats["format_bad"] == 0
    calls_after_first = stub.calls

    cached = translate.cached_translations(PID)
    assert "⟦" not in cached["b1"]["text"] and "[12]" in cached["b1"]["text"]
    assert cached["b1"]["format_ok"] and "8 个 GPU" in cached["b2"]["text"]

    # 第二次：全部命中缓存 → 零 LLM 调用（可中断续译的根基）
    stats2 = translate.translate_blocks(PID, ["b1", "b2"], progress=lambda *a: None)
    assert stats2["cached"] == 2 and stub.calls == calls_after_first


def test_translate_blocks_placeholder_lost_falls_back(monkeypatch, _fresh_translations):
    # 模拟 LLM 丢占位符：回复里没有任何 ⟦n⟧ → restore 失败 → 回退原文 + format_ok=0
    monkeypatch.setattr(llm, "chat", _ContentStub([("encoder", "这句译文把占位符全部弄丢了")]))
    stats = translate.translate_blocks(PID, ["b1"], progress=lambda *a: None)
    assert stats["format_bad"] == 1
    cached = translate.cached_translations(PID)["b1"]
    assert cached["text"] == _DOCIR["blocks"][0]["text"]    # 回退原文
    assert not cached["format_ok"]


def test_translate_blocks_failure_counted(monkeypatch, _fresh_translations):
    def boom(msgs, **kw):
        return {"text": "", "error": "boom"}
    monkeypatch.setattr(llm, "chat", boom)
    stats = translate.translate_blocks(PID, ["b2"], progress=lambda *a: None)
    assert stats["failed"] >= 1


# ---------- 划词翻译 ----------

def test_translate_selection(monkeypatch):
    db.upsert_terminology("BLEU", "BLEU 分数")
    monkeypatch.setattr(llm, "chat", _Stub(["该模型达到 ⟦1⟧ 27.3 的成绩，见 ⟦2⟧。"]))
    out = translate.translate_selection("The model reaches 27.3 [11] BLEU score.")
    assert out["ok"] and "[11]" in out["translation"]
    assert out["term_hit"] and out["term_hit"]["term"] == "BLEU"


def test_translate_selection_unavailable(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda msgs, **kw: {"text": "", "error": "no endpoint"})
    out = translate.translate_selection("hello")
    assert not out["ok"]


# ---------- 端点 ----------

def test_endpoints(monkeypatch, _fresh_translations):
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)

    # 先种入一条缓存（不依赖其他用例的缓存状态）
    monkeypatch.setattr(llm, "chat", _ContentStub([("encoder", "编码器使用 ⟦1⟧ 注意力 ⟦2⟧ 和 ⟦3⟧ 层。")]))
    translate.translate_blocks(PID, ["b1"], progress=lambda *a: None)

    r = client.get(f"/api/papers/{PID}/translation")
    assert r.status_code == 200 and "b1" in r.json()["blocks"]

    r = client.post("/api/terminology", json={"term": "RAG", "translation": "检索增强生成"})
    assert r.status_code == 200 and any(t["term"] == "RAG" for t in r.json()["terms"])

    r = client.post("/api/translate/selection", json={"text": "RAG improves grounding."})
    assert r.status_code == 200

    r = client.delete("/api/terminology/RAG")
    assert r.status_code == 200

    r = client.get("/api/papers/none_xyz/translation")
    assert r.status_code == 200 and r.json()["blocks"] == {}


def test_translate_sse_stream(monkeypatch, _fresh_translations):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(llm, "chat", _ContentStub([("encoder", "译文 ⟦1⟧ ⟦2⟧ ⟦3⟧。"),
                                                   ("GPUs", "块二译文。")]))
    client = TestClient(app)
    events = []
    with client.stream("POST", f"/api/papers/{PID}/translate",
                       json={"lang": "zh", "page": 0}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("event: "):
                events.append(line[7:].strip())
    assert "t_delta" in events and "final" in events and "done" in events
