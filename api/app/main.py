"""FastAPI 入口：uvicorn app.main:app --port 8000"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db, jobs, threads_store
from .agents import recovery
from .api_routes.discover_routes import router as discover_router
from .api_routes.graph_routes import router as graph_router
from .api_routes.routes import router
from .api_routes.threads_routes import router as threads_router
from .api_routes.translate_routes import router as translate_router
from .log import logger
from .log import setup as _setup_log


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _setup_log()
    db.init_db()
    threads_store.init()
    jobs.start()
    n = recovery.start_recovery_worker()  # 断点续跑：恢复上次崩溃/失败遗留的会话线程
    if n:
        logger.info(f"已派发 {n} 个未完成会话线程的后台续跑", module="recovery")
    yield


app = FastAPI(title="科研助手 Agent API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
app.include_router(threads_router)
app.include_router(translate_router)
app.include_router(discover_router)
app.include_router(graph_router)
