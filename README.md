# Meridian — 科研助手 Agent

> 传智杯「AI 大模型创新应用挑战赛」参赛作品 —— PDF 深度解析 + 抗幻觉溯源问答 + 中英联动阅读 + arXiv 检索推荐 + 引用图谱挖掘 + 学术写作 Copilot，六大模块共用一套 **DocIR** 数据底座。

## 项目结构

```
scholar-agent/
├─ api/    # FastAPI 后端：DocIR 解析管道 / 混合检索 / 溯源问答 / 后台任务
├─ web/    # Next.js 前端：文献库 / PDF阅读 / 溯源对话 / 图谱 / 写作
├─ docs/   # 技术文档与 Prompt 审计导出
└─ .env.example
```

## 快速开始

### 后端（FastAPI + uv）

```bash
cd api
uv sync                      # 安装依赖
uv run uvicorn app.main:app --reload --port 8000
```

- 健康检查：`GET http://localhost:8000/api/health`（LLM 连通性真实 ping 检测）
- 完整 API：见 [docs/api.md](docs/api.md)（上传 / 任务进度 / DocIR / SSE 溯源问答 / 审计）

### 前端（Next.js）

```bash
cd web
pnpm install
pnpm build && pnpm start     # http://localhost:3000
```

页面：`/` 文献库（上传+进度条+去重预警） · `/chat` 溯源问答（SSE+ClaimTrace 论断卡片） ·
`/reader/{id}` DocIR 阅读器（标题树+结构块） · `/audit` 透明度中心（Prompt 审计日志）

### 配置

```bash
cp .env.example .env         # 填入密钥（密钥绝不入仓库）
```

当前接入：**火山方舟 Agent Plan**——LLM `doubao-seed-2.1-lite` 与 Embedding
`doubao-embedding-vision` 走订阅专属 OpenAI 兼容面 `/api/plan/v3`（model 直填模型名，
无需 ep 接入点；QA 关闭思考模式提速）。也支持 DeepSeek/MiniMax/任意 OpenAI 兼容端点，
`.env` 换三项即可。Embedding 向量在**索引时**计算落库（方舟单批限 10，代码按 8 分批+退避），
查询时零重算；无 key 时 `EMBEDDING_BACKEND=local` 走 fastembed 本地 BGE-M3，
或纯 BM25 单路降级，检索管线不变。

## 当前模块状态（MVP 闭环已通）

| 模块 | 状态 | 说明 |
|---|---|---|
| 一 PDF 解析 | ✅ | **解析路由**（docs/01 §4.1）：probe→native(MinerU 2.5.4 middle.json)/scanned(Qwen-VL 逐页)/gray(逐页 VLM 救援)/long(50 页/批流式+大纲先出+前 120 页索引)；失败→PyMuPDF 兜底；公式 LaTeX→KaTeX；表格 html+markdown+headers/rows；解析报告六指标（`docs/adr-001-bbox.md` 标定记录） |
| 二 溯源问答 | ✅ | BM25(中文 bigram)+可选 Dense→RRF→Rerank；Evidence Pack `[E#]` 编号引用；L1-L4 Claim-Citation 校验器；ClaimTrace 三态徽章；引用点击跳 PDF bbox 高亮；SSE 流式 |
| 三 阅读器 | ✅ | PDF 原文画布渲染（pdf.js）+ bbox 高亮 + DocIR 文本双视图；`?page&bbox` URL 定位 |
| 四 检索追踪 | ⏳ | 按排期 D9 |
| 五 引用图谱 | ⏳ | 按排期 D7 |
| 六 写作 Copilot | ⏳ | 按排期 D10 |

## 核心机制（对应参赛方案评分点）

| 机制 | 说明 |
|---|---|
| **DocIR 统一底座** | PyMuPDF 解析 → blocks(page,bbox 锚点) / 标题树 / 表格 / 图注 / References，六大模块共用 |
| **混合检索** | SQLite FTS5(BM25，CJK bigram 中文可命中) + 可选 Dense(OpenAI 兼容 embeddings) → RRF(k=60) → bge-reranker 全局精排（失败静默降级融合排序 + guardrail），表格单独成块支撑数值类问题 |
| **四层防御** | ① 混合检索+Rerank ② Evidence Pack（块级 page/bbox/章节，显式编号 E1..En）③ 强制 `[E#]` 引用生成（无效编号剔除+定向重生成环）④ **Claim-Citation 校验器**：L1 编号存在性 / L2 引文原文覆盖率≥0.85（失败重抽）/ L3 数值·型号逐字比对（未命中标「需核对」）/ L4 语义 judge（LLM 批量判定，no→未验证，失败兜底跳过） |
| **ClaimTrace 三态** | 已验证（绿）/ 需核对（黄：数值或引文待人工核对）/ 未验证；`final.anchors` 携带 E#→块级锚点，前端点击弹 PDF 对应页 + bbox 红框高亮 |
| **拒答策略** | 查无依据明确拒答并附「最接近的 3 条证据」；寒暄/范围外拒答；无 LLM 抽取式降级（停用词过滤防噪声命中） |
| **页码口径** | SSE 对外统一 1 基人读页码；DB/DocIR 内部 0 基 |
| **语义去重** | 文件 hash 精确去重 + 标题内容级匹配（官方用例：先 v7 后 v1 触发预警） |
| **后台任务** | 解析走线程队列 + 进度条，541 页超长文档不阻塞 |
| **透明度中心** | `/api/audit/prompts`：每次 LLM 调用的 token/耗时/guardrail 事件全落库 |

## 测试

```bash
cd api && uv run pytest -q
# E2E：上传→解析→索引→溯源问答→去重→审计（6 passed）
# 集成：真实论文 1706.03762v7（无该 PDF 时自动跳过；
#       下载 https://arxiv.org/pdf/1706.03762v7 到 downloads/1706.03762/）
```

## 快速开始

```bash
# 本地开发（Windows / Git Bash）
cd scholar-agent
make install          # api: uv sync · web: pnpm install
cp .env.example .env  # 填 LLM/EMBEDDING key（无 key 也可跑：抽取式降级闭环）
make api              # 终端 1：API :8100
make web              # 终端 2：前端 :3000
```

## 一键演示与验收

```bash
make demo             # docker compose 起全栈（api 健康检查通过后 web 才启动）
make cases            # 官方 3 用例断言式验收（进程内，无需起服务）
make smoke            # 真 HTTP 冒烟（需栈已起）：health→上传→解析→SSE 溯源→翻译→图谱
make test             # 全量门禁：api ruff+pytest · web tsc+lint+build
```

## 工程规范

- **密钥安全**：全部走环境变量（`.env` 不入仓库，`.gitignore` 已封）；`.env.example` 为键位清单；
  `pre-commit install` 后每次提交跑 **gitleaks**（配置 `.gitleaks.toml`）+ ruff。
- **结构化日志**：loguru 双 sink——控制台彩色 + `data/logs/app.log`（JSON 序列化，10MB×5 轮转）；
  uvicorn/fastapi 标准日志统一接管；每次 LLM 调用另落 `qa_logs` 审计（含 prompt 版本号，
  透明度中心 `/audit` 页可看版本分布）。
- **Trace 回放**：每次问答事件流落盘，`GET /api/qa/{trace_id}` 可完整回放（断点续跑并入）。

## 部署（两档）

- **局域网演示档**：评委机同网段直连 `http://<演示机IP>:3000`（http 即可）。
- **线上演示档**：Nginx 反代（`deploy/nginx.conf`，**SSE 必须关缓冲**——
  `proxy_buffering off` 三件套已标红）或 Caddy 自动 HTTPS（`deploy/Caddyfile`）。
- **定时备份**：`scripts/backup.ps1`（data/ + checkpoints → `backup-{date}/`，保留 7 份；
  schtasks 注册命令见脚本头注释）。
- **容器口径**：api 镜像不含 MinerU（torch 过重）——`MINERU_ENABLED=off` 时解析路由自动走
  PyMuPDF 兜底 + 扫描件 VLM 通道，闭环不中断；MinerU 为宿主机增强通道（`MINERU_CMD` 指向
  宿主机 venv）。
