# 科研助手 Agent API（SQLite 单机栈：compose 仅需 api + web 两服务）
# 说明：MinerU 不进镜像（torch 过重）——MINERU_ENABLED=off 时解析路由自动走
# PyMuPDF 兜底 + 扫描件 VLM 通道（dashscope/vlm key 可用）；MinerU 为宿主机增强通道。
FROM python:3.12-slim

# DejaVu 字体：容器内 PDF 渲染/画布缺字防线
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=0 UV_LINK_MODE=copy

# 先装依赖再拷代码（层缓存友好）
COPY pyproject.toml ./
COPY app ./app
RUN uv sync --no-dev --frozen

ENV SCHOLAR_DATA_DIR=/app/data \
    MINERU_ENABLED=off \
    PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
EXPOSE 8100

CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
