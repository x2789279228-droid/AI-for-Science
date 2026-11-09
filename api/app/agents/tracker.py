"""arXiv 动态追踪（模块四 P0-3）：订阅关键词/作者/分类 → 定期增量刷新 → 去重入库。

传输双通道（赛题点名 Sickle，但离线演示不赌单一依赖）：
- 主通道 Sickle（OAI-PMH ListRecords，set=分类，from=水位线增量）——权威收割；
- 回退通道 arXiv Atom API（tools._arxiv_search，sortBy=submittedDate）——Sickle
  未安装/失败/类目不可用时兜底，keyword/author 订阅本就更适合 Atom 检索式。
调度：进程内常驻线程（复用 recovery worker 先例：sleep 循环 + 退出事件），
不引入 APScheduler——无 cron 语义需求，新依赖不值当。水位线持久化在
tracking_subscriptions.last_run：重启从水位续跑，断网错过的窗口自动补拉。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx

from .. import db
from ..config import settings
from ..log import logger

_stop = threading.Event()
_worker: threading.Thread | None = None

_OAI_ENDPOINT = "http://export.arxiv.org/oai2"
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_UA = {"User-Agent": "meridian-scholar-agent/1.0"}


# ---------- 拉取通道 ----------

def _since_date(last_run: float) -> str:
    """水位线 → OAI from 参数（YYYY-MM-DD，UTC）。首次订阅回看 3 天，防巨量收割。"""
    dt = (datetime.fromtimestamp(last_run, tz=UTC) - timedelta(days=1)
          if last_run else datetime.now(tz=UTC) - timedelta(days=3))
    return dt.strftime("%Y-%m-%d")


def _fetch_sickle(set_spec: str, from_date: str, limit: int) -> list[dict] | None:
    """Sickle OAI-PMH 主通道。未安装/失败返回 None（调用方走 Atom 回退）。

    Round4：子类目（cs.CL）不是合法 OAI set（实测 BadArgument: Set does not
    exist）→ 自动降试顶层类目（cs）再收割一轮；两级都失败才交回 Atom 回退。
    """
    try:
        from sickle import Sickle
    except ImportError:
        return None
    last_err = ""
    for spec in (set_spec, set_spec.split(".")[0]):
        try:
            sickle = Sickle(_OAI_ENDPOINT, max_retries=1, timeout=30)
            out: list[dict] = []
            for rec in sickle.ListRecords(metadataPrefix="oai_dc", set=spec,
                                          **{"from": from_date}):
                header = rec.header
                m = re.search(r"arXiv\.org[:/](.+)$", header.identifier)
                if not m or header.deleted:
                    continue
                meta = rec.metadata or {}
                out.append({
                    "arxiv_id": m.group(1).split("v")[0],
                    "title": " ".join(meta.get("title", []) or [""]),
                    "authors": list(meta.get("creator", []) or [])[:8],
                    "published": (meta.get("date", [""]) or [""])[0][:10],
                    # arXiv OAI 的 dc:description 即摘要（推荐 local/keyword 分量语料）
                    "abstract": " ".join((meta.get("description", []) or [""]))[:800],
                })
                if len(out) >= limit:
                    break
            return out
        except Exception as e:  # noqa: BLE001 —— 单个 set 失败：换顶层 set 再试
            last_err = f"{type(e).__name__}: {e}"[:160]
    logger.warning(f"tracking Sickle 通道失败 → Atom 回退: {last_err}", module="tracking")
    return None


def _fetch_atom(query_type: str, value: str, from_date: str, limit: int) -> list[dict]:
    """Atom 回退通道：检索式映射 cat:/au:/all: + sortBy=submittedDate，再按日期过滤。"""
    qmap = {"category": f"cat:{value}", "author": f'au:"{value}"', "keyword": f"all:{value}"}
    resp = httpx.get(
        "http://export.arxiv.org/api/query",
        params={"search_query": qmap.get(query_type, f"all:{value}"), "start": 0,
                "max_results": max(1, min(limit, 20)), "sortBy": "submittedDate",
                "sortOrder": "descending"},
        headers=_UA, timeout=30, follow_redirects=True)   # arXiv http→https 301
    resp.raise_for_status()
    import xml.etree.ElementTree as ET
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(resp.text).findall("a:entry", ns):
        aid = (e.findtext("a:id", "", ns) or "").rsplit("/abs/", 1)[-1]
        out.append({
            "arxiv_id": aid.split("v")[0],
            "title": " ".join((e.findtext("a:title", "", ns) or "").split()),
            "authors": [x.findtext("a:name", "", ns) for x in e.findall("a:author", ns)][:8],
            "published": (e.findtext("a:published", "", ns) or "")[:10],
            "abstract": " ".join((e.findtext("a:summary", "", ns) or "").split())[:800],
        })
    return [o for o in out if o["published"] >= from_date] or out  # 日期过滤失败面=空时如实返回全部（首次订阅）


def _keyword_filter(items: list[dict], keyword: str) -> list[dict]:
    k = keyword.lower()
    return [i for i in items if k in i["title"].lower()
            or any(k in (a or "").lower() for a in i["authors"])]


# ---------- 刷新与入库 ----------

def refresh_subscription(sub: dict) -> dict:
    """刷新单个订阅：拉取 → keyword 过滤 → 去重入库（含库内已知标记）→ 推进水位线。"""
    limit = settings.tracking_max_items
    from_date = _since_date(sub["last_run"])
    channel = "atom"
    items: list[dict] = []
    if sub["query_type"] == "category":
        got = _fetch_sickle(sub["value"], from_date, limit)
        if got is not None:
            channel = "sickle"
            items = got
    if not items:
        items = _fetch_atom(sub["query_type"], sub["value"], from_date, limit)
    if sub["query_type"] == "keyword":
        items = _keyword_filter(items, sub["value"])

    known = {r["arxiv_id"] for r in db.query("SELECT arxiv_id FROM papers WHERE arxiv_id!=''")}
    added = skipped = 0
    now = time.time()
    for it in items[:limit]:
        status = "known" if it["arxiv_id"] in known else "new"
        with db.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO tracked_items(id,sub_id,arxiv_id,title,published,"
                "authors,abstract,status,added_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (db.new_id("ti_"), sub["id"], it["arxiv_id"], it["title"][:300],
                 it["published"], json.dumps(it["authors"], ensure_ascii=False),
                 (it.get("abstract") or "")[:800], status, now))
            added += cur.rowcount or 0
            if not (cur.rowcount or 0) and it.get("abstract"):
                # 去重跳过的旧条目：若早期入库无摘要则补齐（元数据闭环）
                c.execute("UPDATE tracked_items SET abstract=? WHERE sub_id=? AND arxiv_id=?"
                          " AND (abstract IS NULL OR abstract='')",
                          (it["abstract"][:800], sub["id"], it["arxiv_id"]))
            skipped += 0 if (cur.rowcount or 0) else 1
    with db.tx() as c:
        c.execute("UPDATE tracking_subscriptions SET last_run=? WHERE id=?", (now, sub["id"]))
    logger.info(f"tracking[{sub['name']}] +{added} 新条目（{channel} 通道，去重 {skipped}）",
                module="tracking")
    return {"sub_id": sub["id"], "channel": channel, "fetched": len(items),
            "added": added, "deduped": skipped}


def refresh_all(sub_id: str | None = None) -> list[dict]:
    rows = db.query("SELECT * FROM tracking_subscriptions WHERE enabled=1"
                    + (" AND id=?" if sub_id else ""), (sub_id,) if sub_id else ())
    return [refresh_subscription(dict(r)) for r in rows]


# ---------- 后台调度（常驻线程：间隔轮询 + 水位线续跑） ----------

def start_tracking_worker() -> bool:
    """启动常驻刷新线程（幂等；tracking_interval_s=0 关闭定时）。"""
    global _worker
    if settings.tracking_interval_s <= 0 or (_worker and _worker.is_alive()):
        return False
    _stop.clear()

    def loop() -> None:
        while not _stop.wait(settings.tracking_interval_s):
            try:
                refresh_all()
            except Exception as e:  # noqa: BLE001 —— 单轮失败不断链，下轮重试
                logger.warning(f"tracking 定时刷新失败: {type(e).__name__}: {e}",
                               module="tracking")

    _worker = threading.Thread(target=loop, daemon=True, name="arxiv-tracking")
    _worker.start()
    return True


def stop_tracking_worker() -> None:
    _stop.set()


# ---------- 查询助手 ----------

def list_items(sub_id: str | None = None, status: str | None = None,
               limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM tracked_items"
    conds, args = [], []
    if sub_id:
        conds.append("sub_id=?"); args.append(sub_id)
    if status:
        conds.append("status=?"); args.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY added_at DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    return [dict(r) | {"authors": json.loads(r["authors"] or "[]")} for r in db.query(sql, tuple(args))]


def dismiss_item(item_id: str) -> bool:
    with db.tx() as c:
        cur = c.execute("UPDATE tracked_items SET status='dismissed' WHERE id=?", (item_id,))
        return bool(cur.rowcount)
