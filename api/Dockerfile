# 科研助手 Agent API（SQLite 单机栈：compose 仅需 api + web 两服务）
# 说明：MinerU 不进镜像（torch 过重）——MINERU_ENABLED=off 时解析路由自动走
# PyMuPDF 兜底 + 扫描件 VLM 通道（dashscope/vlm key 可用）；MinerU 为宿主机增强通道。
FROM python:3.12-slim

# DejaVu（拉丁）+ WenQuanYi（CJK）：容器内 PDF 渲染/画布/matplotlib PNG 缺字防线——
# 无 CJK 字体时中文渲染为方框（评测报告/拓扑图/沙箱出图均受影响）
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=0 UV_LINK_MODE=copy

# 先装依赖再拷代码（层缓存友好）；uv sync --frozen 需要 uv.lock 在场（P0-4）。
# --extra sandbox 装 matplotlib（python_sandbox 绘图 / 拓扑 PNG 演示必需）
# 国内构建可经 compose args 注入镜像源（如清华 TUNA），默认官方 PyPI
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
ENV UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX}
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY scripts ./scripts
RUN uv sync --no-dev --frozen --extra sandbox

ENV SCHOLAR_DATA_DIR=/app/data \
    MINERU_ENABLED=off \
    PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
EXPOSE 8100

# --no-sync：运行期不再重算环境（否则 uv run 会按无 extras 的 lock 剥掉 matplotlib）
CMD ["uv", "run", "--no-dev", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
