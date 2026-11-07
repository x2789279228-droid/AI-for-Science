"""FastAPI 入口：uvicorn app.main:app --port 8000"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db, jobs
from .api_routes.routes import router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    jobs.start()
    yield


app = FastAPI(title="科研助手 Agent API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
