"""FastAPI 入口：uvicorn app.main:app --port 8000

Round4 横切生产化：
- RequestId + 限流 + 请求计数中间件（X-Request-Id 回写；重端点 POST 令牌桶限流
  429+Retry-After；进程内计数供 /api/metrics）
- GET /api/metrics：请求计数（内存）+ LLM/任务/翻译（qa_logs 等落库聚合）+ 进程资源
"""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db, jobs, threads_store
from .agents import recovery
from .agents import tracker as tracker_worker
from .api_routes.copilot_routes import router as copilot_router
from .api_routes.discover_routes import router as discover_router
from .api_routes.export_routes import router as export_router
from .api_routes.graph_routes import router as graph_router
from .api_routes.recommend_routes import router as recommend_router
from .api_routes.routes import router
from .api_routes.threads_routes import router as threads_router
from .api_routes.tracking_routes import router as tracking_router
from .api_routes.translate_routes import router as translate_router
from .config import settings
from .log import logger
from .log import setup as _setup_log
from .ratelimit import RateLimiter

_STARTED_AT = time.time()
_requests_lock = threading.Lock()
_requests = {"total": 0, "rate_limited": 0, "in_flight": 0,
             "by_status": {}}
# 密闭测试（conftest）置 RATE_LIMIT_PER_MIN=0 关闭；宿主 .env 缺省时生产默认 30
_limiter = RateLimiter(settings.rate_limit_per_min)

# 重端点：LLM/外部 API/解析密集的 POST（限流只防滥用，不拦只读 GET）
_HEAVY_POST_PREFIXES = (
    "/api/chat", "/api/papers", "/api/translate", "/api/copilot",
    "/api/discover", "/api/graph", "/api/ideas", "/api/future-checks",
    "/api/recommendations", "/api/subscriptions", "/api/threads",
)


def _is_heavy_post(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _HEAVY_POST_PREFIXES)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _setup_log()
    db.init_db()
    threads_store.init()
    jobs.start()
    _limiter.rate = max(0, int(settings.rate_limit_per_min))   # 与导入期同源（env 可调）
    n = recovery.start_recovery_worker()  # 断点续跑：恢复上次崩溃/失败遗留的会话线程
    if n:
        logger.info(f"已派发 {n} 个未完成会话线程的后台续跑", module="recovery")
    if tracker_worker.start_tracking_worker():  # 模块四 P0-3：arXiv 定时追踪
        logger.info(f"arXiv 追踪 worker 已启动（间隔 {tracker_worker.settings.tracking_interval_s}s）",
                    module="tracking")
    yield
    tracker_worker.stop_tracking_worker()


app = FastAPI(title="科研助手 Agent API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _request_id_and_ratelimit(request: Request, call_next):
    """① X-Request-Id（透传或生成，回写响应+日志绑定）；
    ② 重端点 POST 令牌桶限流（匿名 X-User-Id / client host 分桶，429+Retry-After）；
    ③ 进程内请求计数（/api/metrics 数据源）。"""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    with _requests_lock:
        _requests["total"] += 1
        _requests["in_flight"] += 1
    t0 = time.monotonic()
    try:
        if request.method == "POST" and _limiter.rate > 0 and _is_heavy_post(request.url.path):
            key = request.headers.get("x-user-id") or \
                (request.client.host if request.client else "anonymous")
            if key == "testclient":
                key = ""   # 进程内验收脚本（TestClient host 恒为 testclient）豁免：
                           # 限流对象是网络滥用方，不是自家 eval/bench（e2e 走真端口照常受限）
            ok, retry = (True, 0.0) if not key else _limiter.allow(key)
            if not ok:
                with _requests_lock:
                    _requests["rate_limited"] += 1
                    _requests["by_status"]["429"] = _requests["by_status"].get("429", 0) + 1
                    _requests["in_flight"] -= 1
                logger.warning(f"rate_limited {key} {request.url.path}", module="ratelimit",
                               request_id=rid)
                return JSONResponse({"detail": "rate_limited"},
                                    status_code=429,
                                    headers={"Retry-After": str(max(1, int(retry) + 1)),
                                             "X-RateLimit-Limit": str(_limiter.rate),
                                             "X-RateLimit-Remaining": "0",
                                             "X-Request-Id": rid})
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        with _requests_lock:
            _requests["by_status"][str(response.status_code)] = \
                _requests["by_status"].get(str(response.status_code), 0) + 1
        return response
    finally:
        with _requests_lock:
            _requests["in_flight"] -= 1
        logger.bind(request_id=rid).debug(
            f"{request.method} {request.url.path} {(time.monotonic() - t0) * 1000:.0f}ms",
            module="http")


@app.get("/api/metrics")
def get_metrics():
    """运行指标：内存请求计数 + 落库聚合（LLM/任务/翻译）+ 进程资源。只读、无敏感信息。"""
    with _requests_lock:
        req = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _requests.items()}
    llm = db.query("SELECT COUNT(*) c, COALESCE(SUM(prompt_tokens),0) pt,"
                   " COALESCE(SUM(completion_tokens),0) ct,"
                   " COALESCE(AVG(latency_ms),0) avg_ms FROM qa_logs")[0]
    jobs_stat = {"done": 0, "error": 0, "queued": 0}
    for r in db.query("SELECT status, COUNT(*) c FROM jobs GROUP BY status"):
        if r["status"] in jobs_stat:
            jobs_stat[r["status"]] = r["c"]
    papers = db.query("SELECT COUNT(*) c FROM papers WHERE status='active'")[0]["c"]
    translations = db.query("SELECT COUNT(*) c FROM translations")[0]["c"]
    proc = {"rss_mb": 0.0, "cpu_percent": 0.0}
    try:
        import psutil
        p = psutil.Process()
        proc = {"rss_mb": round(p.memory_info().rss / 1e6, 1), "cpu_percent": p.cpu_percent(0.0)}
    except Exception as e:  # noqa: BLE001 —— psutil 不可用则如实为 0（不阻断指标）
        logger.debug(f"psutil 不可用：{type(e).__name__}", module="metrics")
    return {"uptime_s": round(time.time() - _STARTED_AT, 0),
            "rate_limit_per_min": _limiter.rate,
            "requests": req, "llm": {"calls": llm["c"], "prompt_tokens": llm["pt"],
                                     "completion_tokens": llm["ct"],
                                     "avg_latency_ms": round(llm["avg_ms"], 0)},
            "jobs": jobs_stat, "papers": papers, "translations": translations,
            "process": proc}


app.include_router(router)
app.include_router(threads_router)
app.include_router(translate_router)
app.include_router(discover_router)
app.include_router(graph_router)
app.include_router(export_router)
app.include_router(tracking_router)
app.include_router(recommend_router)
app.include_router(copilot_router)
