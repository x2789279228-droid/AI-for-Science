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
CHECKPOINT_DB_PATH = DATA_DIR / "checkpoints.db"  # LangGraph SqliteSaver 断点续跑存储

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
    # 火山方舟 doubao-seed 系列默认开思考；QA 场景置 1 关闭（thinking={"type":"disabled"}）
    llm_thinking_disabled: bool = False

    # Embedding / Rerank
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_backend: str = "auto"  # auto | api | local | off
    rerank_api_key: str = ""
    rerank_base_url: str = "https://api.siliconflow.cn/v1"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # 多模态兜底
    dashscope_api_key: str = ""

    # 解析路由：MinerU 原生通道（独立 venv 的 mineru CLI；auto=检测到才启用）。
    # venv 必须在纯 ASCII 路径：fast_langdetect 的 fasttext C 库在 Windows 上
    # 打不开含中文的 site-packages 路径（见 docs/adr-001-bbox.md）
    mineru_enabled: str = "auto"   # auto | on | off
    mineru_cmd: str = r"D:\mineru-venv\Scripts\mineru.exe"
    mineru_timeout_s: int = 1800   # 单次解析（或长文档单批）超时
    # VLM 扫描件通道：默认跟随 LLM 端点（火山方舟 token plan 的 doubao-seed 系列本身
    # 支持图像输入，实测整页版面识读准确）；也可显式指向 DashScope qwen-vl-max
    vlm_base_url: str = ""         # 空 → 用 llm_base_url
    vlm_api_key: str = ""          # 空 → 用 llm_api_key
    vlm_model: str = ""            # 空 → 用 llm_model
    vlm_enabled: bool = True       # 0 关闭（扫描件回退 MinerU OCR / 诚实标注）
    # 长文档：全文 chunk/索引的页数上限（超出部分仅入 DocIR 按需阅读）
    long_doc_index_pages: int = 120
    # 长文档批切页 MinerU：每段页数（0=关闭批切、维持整本单次调用）
    mineru_chunk_pages: int = 100
    # 语义去重综合分阈值（scripts/calibrate_dedup.py 扫 0.80~0.95 标定）
    dedup_threshold: float = 0.85
    # 去重 cross-encoder 精判：每次扫描最多精判的对数（blocking 后候选通常 <10）
    dedup_rerank_max: int = 10
    # 去重 LLM 灰区复核（默认开）：只对规则分最不可信的对调用，≤上限次/扫描
    dedup_llm_judge: bool = True
    dedup_llm_judge_max: int = 6
    # 灰区带宽：阈值下方该带宽内的近失对触发 LLM 复核（补警/否决）
    dedup_gray_zone: float = 0.10
    # 批量上传单次文件数上限
    batch_upload_max: int = 20

    # 运行时
    daily_llm_limit: int = 2000
    api_port: int = 8000
    # L4 语义支持 judge（verify.py）：批量一次调用判 claim 能否被证据推出；0 关闭
    semantic_judge: bool = True
    # Prompt A/B 开关（app/prompts/*.yaml 的 variants 档位）：default | strict，运行时可切
    prompt_variant: str = "default"
    # GitHub 仓库搜索（4.4 复现性分）；无 token 时 GitHub API 限流 10 次/分，如实降级
    github_token: str = ""

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
        backend = self.embedding_backend
        if backend == "off":
            return False
        if backend == "api":
            return bool(self.embedding_api_key)
        if backend == "local":
            return True
        # auto：有 API key 走 API，否则尝试本地 fastembed
        return bool(self.embedding_api_key) or _fastembed_available()


def _fastembed_available() -> bool:
    try:
        import fastembed  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 —— 依赖探测失败视为不可用
        return False


settings = Settings()
