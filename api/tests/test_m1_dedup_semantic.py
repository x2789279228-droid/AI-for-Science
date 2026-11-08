"""模块一 P0 语义去重扩展：相似度数值/分量、三态决策软归档、TOCTOU 并发、CRUD 级联、取消。

官方用例 3.1 的断言加强版（分数数值 + 分量 breakdown + 三按钮决策 + 软归档过滤）。
"""
from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, jobs
from app.parsing._types import ParseCancelled

_DATA = Path(__import__("os").environ["SCHOLAR_DATA_DIR"])


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


def _arxiv_base(seed: str) -> str:
    """按种子派生稳定异族 arXiv 基 id——测试素材同 id 会触发「id 同源」判定而互配。"""
    import zlib

    return f"1707.{zlib.crc32(seed.encode('utf-8')) % 100000:05d}"


def _make_pdf(path: Path, title: str, ver: int, fact: str,
              base: str | None = None) -> Path:
    from tests.fixtures_pdf import make_paper_pdf

    base = base or _arxiv_base(title)
    make_paper_pdf(path, title,
                   body_facts=[fact],
                   refs=[f"Vaswani et al. 2017. arXiv:{base}"],
                   arxiv_line=f"arXiv:{base}v{ver}")
    return path


def _upload(client: TestClient, path: Path) -> dict:
    r = client.post("/api/papers",
                    files={"file": (path.name, io.BytesIO(path.read_bytes()), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def _wait(client: TestClient, job_id: str, timeout_s: float = 120) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            return j
        time.sleep(0.2)
    return {"status": "timeout"}


_PAIR_SEQ = iter(range(1, 1000))   # 同 tag 多次调用（fixture 函数级作用域）每对唯一，
# 防跨测试互撞（旧实现 sd2 三对同 base 同题互配，制造大量假 pending）


def _pair(tag: str) -> tuple[Path, Path]:
    tmp = _DATA / tag
    tmp.mkdir(parents=True, exist_ok=True)
    seq = next(_PAIR_SEQ)
    base = _arxiv_base(f"pair-{tag}-{seq}")
    v7 = _make_pdf(tmp / f"{tag}{seq}_{base}v7.pdf", f"Semantic Dedup Paper {tag} No{seq}", 7,
                   f"Variant {tag}{seq} v7 trains on 8 GPUs reaching 27.3 BLEU on WMT14.",
                   base=base)
    v1 = _make_pdf(tmp / f"{tag}{seq}_{base}v1.pdf", f"Semantic Dedup Paper {tag} No{seq}", 1,
                   f"Variant {tag}{seq} v1 trains on 8 GPUs reaching 26.1 BLEU on WMT14.",
                   base=base)
    return v7, v1


def _alerts(client: TestClient, paper_id: str) -> list[dict]:
    return client.get(f"/api/papers/{paper_id}").json().get("dedup_alerts", [])


# ---------- 相似度数值 + 分量 breakdown ----------

def test_dedup_score_breakdown_and_version_advice():
    client = _client()
    v7p, v1p = _pair("sd1")
    p7 = _upload(client, v7p)
    assert _wait(client, p7["job_id"])["status"] == "done"
    p1 = _upload(client, v1p)
    assert _wait(client, p1["job_id"])["status"] == "done"

    alerts = _alerts(client, p1["paper_id"])
    assert alerts, "同族不同版本必须触发内容级预警"
    top = alerts[0]
    assert top["score"] >= 0.85, f"综合分应过阈值：{top['score']}"
    assert "综合相似度" in top["reason"] and "建议保留" in top["reason"] \
        and "v7" in top["reason"], "官方用例 3.1 文案：相似度数值 + 版本号 + 保留建议"
    bd = top["breakdown"]
    assert abs(bd.get("title", 0) - 1.0) < 0.05, "标题相似度应接近 1"
    assert "minhash" in bd, "正文指纹分量应在场"
    assert "vector" not in bd, "测试密闭无 embedding → 向量分量缺失应按权重归一化（不伪造 0 分）"
    assert top.get("duplicate_of") == p7["paper_id"], "旧口径别名兼容"
    # 对称补警：先完成解析的 v7 侧也有指向 v1 的 pending 预警
    alerts7 = _alerts(client, p7["paper_id"])
    assert any(a["other_paper_id"] == p1["paper_id"] for a in alerts7), "TOCTOU 防线二：对称补警"


def test_different_papers_no_alert():
    client = _client()
    tmp = _DATA / "sd_diff"
    tmp.mkdir(parents=True, exist_ok=True)
    a = _make_pdf(tmp / "paper_a.pdf", "Graph Neural Networks Survey", 1,
                  "We survey message passing architectures.")
    b = _make_pdf(tmp / "paper_b.pdf", "Reinforcement Learning for Robotics", 1,
                  "We review policy gradient methods in manipulation.")
    pa = _upload(client, a)
    _wait(client, pa["job_id"])
    pb = _upload(client, b)
    _wait(client, pb["job_id"])
    assert not _alerts(client, pa["paper_id"]), "异文不得误报"
    assert not _alerts(client, pb["paper_id"])


# ---------- 三态决策（软归档语义） ----------

@pytest.fixture()
def _pair_ready():
    client = _client()
    v7p, v1p = _pair("sd2")
    p7 = _upload(client, v7p)
    _wait(client, p7["job_id"])
    p1 = _upload(client, v1p)
    _wait(client, p1["job_id"])
    return client, p7["paper_id"], p1["paper_id"]


def test_decision_keep_archives_new(_pair_ready):
    client, p7, p1 = _pair_ready
    r = client.post(f"/api/papers/{p1}/dedup-decision",
                    json={"decision": "keep", "target_id": p7})
    assert r.status_code == 200, r.text
    assert r.json()["new"]["status"] == "archived"
    # 幂等
    r2 = client.post(f"/api/papers/{p1}/dedup-decision",
                     json={"decision": "keep", "target_id": p7})
    assert r2.status_code == 200
    # 默认列表不含归档，include_archived=1 含
    ids = {p["id"] for p in client.get("/api/papers").json()["papers"]}
    assert p1 not in ids and p7 in ids
    ids_all = {p["id"] for p in
               client.get("/api/papers?include_archived=1").json()["papers"]}
    assert {p1, p7} <= ids_all
    # 该对的预警必须落终态（成对级别：会话 DB 共享，跨对 pending 不在本测范围）
    n = db.query("SELECT COUNT(*) c FROM dedup_alerts WHERE status='pending' AND "
                 "((new_paper_id=? AND existing_paper_id=?) OR "
                 "(new_paper_id=? AND existing_paper_id=?))",
                 (p1, p7, p7, p1))[0]["c"]
    assert n == 0, "已决策的预警必须落终态"
    # 恢复
    assert client.post(f"/api/papers/{p1}/restore").status_code == 200
    ids = {p["id"] for p in client.get("/api/papers").json()["papers"]}
    assert p1 in ids


def test_decision_cover_superseded_by(_pair_ready):
    client, p7, p1 = _pair_ready
    r = client.post(f"/api/papers/{p1}/dedup-decision",
                    json={"decision": "cover", "target_id": p7})
    assert r.status_code == 200
    body = r.json()
    assert body["target"]["status"] == "archived" and \
        body["target"]["superseded_by"] == p1, "覆盖：旧版归档并指向新版"
    assert body["new"]["supersedes"] == p7
    ids = {p["id"] for p in client.get("/api/papers").json()["papers"]}
    assert p1 in ids and p7 not in ids


def test_decision_coexist_family_linked(_pair_ready):
    client, p7, p1 = _pair_ready
    r = client.post(f"/api/papers/{p1}/dedup-decision",
                    json={"decision": "coexist", "target_id": p7})
    assert r.status_code == 200
    body = r.json()
    assert body["new"]["status"] == "active" and body["target"]["status"] == "active"
    assert body["new"]["family_id"] and body["new"]["family_id"] == body["target"]["family_id"]
    n = db.query("SELECT COUNT(*) c FROM dedup_alerts WHERE status='pending' AND "
                 "((new_paper_id=? AND existing_paper_id=?) OR "
                 "(new_paper_id=? AND existing_paper_id=?))",
                 (p1, p7, p7, p1))[0]["c"]
    assert n == 0, "并存决策后该对预警必须落终态"


def test_decision_rejects_unrelated(client=None):
    client = _client()
    tmp = _DATA / "sd_unrel"
    tmp.mkdir(parents=True, exist_ok=True)
    a = _make_pdf(tmp / "ua.pdf", "Graph Neural Networks Survey", 1, "Alpha body fact.")
    b = _make_pdf(tmp / "ub.pdf", "Bayesian Optimization Handbook", 1, "Beta body fact.")
    # 显式异族：arXiv id 不同（默认 arxiv_line 同 id 会撞 family）
    from tests.fixtures_pdf import make_paper_pdf as _mk
    _mk(a, "Graph Neural Networks Survey", body_facts=["Alpha body fact."],
        refs=["R1 et al. 2020. arXiv:2001.00001"], arxiv_line="arXiv:2001.00001v1")
    _mk(b, "Bayesian Optimization Handbook", body_facts=["Beta body fact."],
        refs=["R2 et al. 2021. arXiv:2002.00002"], arxiv_line="arXiv:2002.00002v1")
    pa = _upload(client, a)
    _wait(client, pa["job_id"])
    pb = _upload(client, b)
    _wait(client, pb["job_id"])
    r = client.post(f"/api/papers/{pb['paper_id']}/dedup-decision",
                    json={"decision": "coexist", "target_id": pa["paper_id"]})
    assert r.status_code == 400, "非同族禁止决策"


# ---------- TOCTOU：并发上传同族 ----------

def test_concurrent_upload_symmetric_alerts():
    client = _client()
    v7p, v1p = _pair("sd3")
    results: list[dict] = [None, None]  # type: ignore[list-item]

    def up(idx: int, path: Path) -> None:
        c = _client()
        results[idx] = _upload(c, path)

    t7 = threading.Thread(target=up, args=(0, v7p))
    t1 = threading.Thread(target=up, args=(1, v1p))
    t7.start(); t1.start(); t7.join(); t1.join()
    for r in results:
        assert _wait(client, r["job_id"])["status"] == "done"
    a7 = _alerts(client, results[0]["paper_id"])
    a1 = _alerts(client, results[1]["paper_id"])
    assert a7 and a1, "并发上传同族：双方都必须有预警（TOCTOU 防线一+二）"
    pid7, pid1 = results[0]["paper_id"], results[1]["paper_id"]
    assert any(a["other_paper_id"] == pid1 for a in a7), "v7 侧预警指向 v1"
    assert any(a["other_paper_id"] == pid7 for a in a1), "v1 侧预警指向 v7"


# ---------- CRUD：删除级联 / 重解析 / 改名 ----------

def test_delete_cascade():
    client = _client()
    v7p, v1p = _pair("sd4")
    p7 = _upload(client, v7p)
    _wait(client, p7["job_id"])
    p1 = _upload(client, v1p)
    _wait(client, p1["job_id"])
    r = client.delete(f"/api/papers/{p1['paper_id']}")
    assert r.status_code == 200
    assert client.get(f"/api/papers/{p1['paper_id']}").status_code == 404
    pid = p1["paper_id"]
    assert not db.query("SELECT 1 FROM chunks WHERE paper_id=?", (pid,)), "chunks 级联清理"
    assert not db.query("SELECT 1 FROM chunks_fts WHERE paper_id=?", (pid,)), "FTS 级联清理"
    assert not db.query("SELECT 1 FROM dedup_alerts WHERE new_paper_id=? OR existing_paper_id=?",
                        (pid, pid)), "双向预警级联清理"
    assert db.query("SELECT 1 FROM papers WHERE id=?", (p7["paper_id"],)), "他篇不受影响"
    assert client.delete(f"/api/papers/{pid}").status_code == 404


def test_reparse_chunks_not_duplicated():
    client = _client()
    tmp = _DATA / "sd5"
    tmp.mkdir(parents=True, exist_ok=True)
    a = _make_pdf(tmp / "rp.pdf", "Reparse Target Paper", 1, "Some unique body fact.")
    p = _upload(client, a)
    assert _wait(client, p["job_id"])["status"] == "done"
    pid = p["paper_id"]
    n1 = db.query("SELECT COUNT(*) c FROM chunks WHERE paper_id=?", (pid,))[0]["c"]
    assert n1 > 0
    r = client.post(f"/api/papers/{pid}/reparse")
    assert r.status_code == 200
    assert _wait(client, r.json()["job_id"])["status"] == "done"
    n2 = db.query("SELECT COUNT(*) c FROM chunks WHERE paper_id=?", (pid,))[0]["c"]
    assert n1 == n2, "重解析不得重复入库（chunks 应自清）"


def test_rename_syncs_docir():
    client = _client()
    tmp = _DATA / "sd6"
    tmp.mkdir(parents=True, exist_ok=True)
    a = _make_pdf(tmp / "rn.pdf", "Original Title Paper", 1, "Body fact for rename.")
    p = _upload(client, a)
    _wait(client, p["job_id"])
    r = client.patch(f"/api/papers/{p['paper_id']}", json={"title": "Renamed Title Paper"})
    assert r.status_code == 200
    assert client.get(f"/api/papers/{p['paper_id']}").json()["title"] == "Renamed Title Paper"
    meta = client.get(f"/api/papers/{p['paper_id']}/docir?outline=1").json()["meta"]
    assert meta["title"] == "Renamed Title Paper", "DocIR meta.title 同步"
    # 空标题拒绝
    assert client.patch(f"/api/papers/{p['paper_id']}",
                        json={"title": "  "}).status_code == 400


# ---------- 任务取消 ----------

def test_cancel_parse_job(monkeypatch):
    """协作式取消：引擎在 cancel_check 命中时抛 ParseCancelled → job 落 cancelled。"""
    from app.parsing import pipeline as pl

    def slow_parse(pdf_path, progress=None, paper_id="", cancel_check=None):
        t0 = time.time()
        while time.time() - t0 < 15:
            if cancel_check is not None and cancel_check():
                raise ParseCancelled("取消测试")
            time.sleep(0.05)
        return pl.parse_pdf(pdf_path)

    monkeypatch.setattr(jobs, "parse_document", slow_parse)
    client = _client()
    tmp = _DATA / "sd7"
    tmp.mkdir(parents=True, exist_ok=True)
    a = _make_pdf(tmp / "cc.pdf", "Cancel Target Paper", 1, "Body fact for cancel.")
    p = _upload(client, a)
    time.sleep(0.5)   # 让 job 进入 running
    r = client.post(f"/api/jobs/{p['job_id']}/cancel")
    assert r.status_code == 200
    j = _wait(client, p["job_id"])
    assert j["status"] == "cancelled", f"应落 cancelled 终态：{j}"
