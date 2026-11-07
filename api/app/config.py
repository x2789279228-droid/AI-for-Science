"""全局配置：环境变量驱动，密钥绝不落仓库（只读 .env / 进程环境）。"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]  # scholar-agent/
DATA_DIR = Path(os.environ.get("SCHOLAR_DATA_DIR", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DOCIR_DIR = DATA_DIR / "docir"
DB_PATH = DATA_DIR / "scholar.db"

for d in (DATA_DIR, UPLOAD_DIR, DOCIR_DIR):
    d.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # LLM
    llm_provider: str = ""  # deepseek | minimax | custom | ""(自动探测)
    deepseek_api_key: str = ""
    minimax_api_key: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_reasoning_model: str = ""
    llm_temperature: float = 0.1

    # Embedding / Rerank
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_backend: str = "auto"  # auto | api | off
    rerank_api_key: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # 多模态兜底
    dashscope_api_key: str = ""

    # 运行时
    daily_llm_limit: int = 2000
    api_port: int = 8000

    # --- 派生属性 ---

    @property
    def llm_endpoint(self) -> tuple[str, str, str] | None:
        """返回 (base_url, api_key, default_model)；无可用配置返回 None。"""
        provider = self.llm_provider or "auto"
        if provider == "deepseek" and self.deepseek_api_key:
            return "https://api.deepseek.com", self.deepseek_api_key, self.llm_model or "deepseek-chat"
        if provider == "minimax" and self.minimax_api_key:
            return "https://api.minimax.io/v1", self.minimax_api_key, self.llm_model or "MiniMax-M2"
        if provider == "custom" and self.llm_base_url and self.llm_api_key:
            return self.llm_base_url.rstrip("/"), self.llm_api_key, self.llm_model or "gpt-4o-mini"
        # auto：按 key 存在性探测
        if self.deepseek_api_key:
            return "https://api.deepseek.com", self.deepseek_api_key, self.llm_model or "deepseek-chat"
        if self.minimax_api_key:
            return "https://api.minimax.io/v1", self.minimax_api_key, self.llm_model or "MiniMax-M2"
        if self.llm_base_url and self.llm_api_key:
            return self.llm_base_url.rstrip("/"), self.llm_api_key, self.llm_model or "gpt-4o-mini"
        return None

    @property
    def dense_enabled(self) -> bool:
        return bool(self.embedding_api_key) and self.embedding_backend != "off"


settings = Settings()
