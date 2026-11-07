# 科研助手 Agent（Scholar Agent）

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

- 健康检查：`GET http://localhost:8000/api/health`
- 完整 API：见 `docs/api.md`（上传 / 任务进度 / DocIR / SSE 溯源问答 / 审计）

### 前端（Next.js）

```bash
cd web
pnpm install
pnpm dev                     # http://localhost:3000
```

### 配置

```bash
cp .env.example .env         # 填入 DEEPSEEK_API_KEY 等密钥（密钥绝不入仓库）
```

无 LLM key 时系统自动降级为**抽取式证据模式**：检索与逐论断溯源照常工作，
答案为原文摘录并逐句挂页码锚点；配置 key 后无缝升级为生成式溯源问答。

## 核心机制（对应参赛方案评分点）

| 机制 | 说明 |
|---|---|
| **DocIR 统一底座** | PyMuPDF 解析 → blocks(page,bbox 锚点) / 标题树 / 表格 / 图注 / References，六大模块共用 |
| **混合检索** | SQLite FTS5(BM25) + 可选 Dense(OpenAI 兼容 embeddings) → RRF 融合，检索通道透明可查 |
| **抗幻觉七道防线** | 强制引用 `[^p页-§章节]` → 引用后验校验（无效锚点剔除/重试）→ ClaimTrace 逐论断拆解 → 查无依据明确拒答 → 全量审计日志 |
| **语义去重** | 文件 hash 精确去重 + 标题内容级匹配（官方用例：先 v7 后 v1 触发预警） |
| **后台任务** | 解析走线程队列 + 进度条，541 页超长文档不阻塞 |
| **透明度中心** | `/api/audit/prompts`：每次 LLM 调用的 token/耗时/guardrail 事件全落库 |

## 测试

```bash
cd api && uv run pytest -q   # E2E：上传→解析→索引→溯源问答→去重→审计 全链路
```
