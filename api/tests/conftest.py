"""会话级密闭夹具：在**任何 app 模块导入之前**固定本 pytest 进程的唯一数据目录。

此前各测试模块用 setdefault 各自设 SCHOLAR_DATA_DIR，但 app.config 只在首次导入时读 env
——整个进程实际共用第一个导入者的目录，跨模块/跨次运行数据互相污染（如 insights 的
pA「Attention Is All You Need」被 e2e 去重用例误匹配）。conftest 先于收集执行，彻底隔离。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

_SESSION_DIR = Path(__file__).parent / f"_ses_{uuid.uuid4().hex[:8]}"
os.environ["SCHOLAR_DATA_DIR"] = str(_SESSION_DIR)

# 凭据密闭（本机全局 env 泄漏史：MINIMAX_API_KEY 全局存在，必须**直接赋值**清空，
# setdefault 对已存在的全局 key 无效——test_4_audit_log 曾因此 llm_configured=True）
for _k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_PROVIDER", "DEEPSEEK_API_KEY",
           "MINIMAX_API_KEY", "DASHSCOPE_API_KEY"):
    os.environ[_k] = ""
os.environ.setdefault("MINERU_ENABLED", "off")
os.environ.setdefault("EMBEDDING_BACKEND", "off")

# 上一批会话目录清理（保留最近 3 个，防积盘）
def _prune_old_sessions() -> None:
    import shutil

    try:
        sessions = sorted(Path(__file__).parent.glob("_ses_*"), key=lambda p: p.stat().st_mtime)
        for old in sessions[:-3]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass


_prune_old_sessions()

# 导入 app 触发 config 求 env（在唯一目录下建 data 结构）
from app import db as _db
from app.config import DATA_DIR as _DATA_DIR

assert str(_DATA_DIR).startswith(str(_SESSION_DIR)), "数据目录隔离失效"
_db.init_db()
