# API 一览（MVP）

Base URL：`http://localhost:8100`（`API_PORT` 可改）

## 文献

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/papers` | multipart 上传 PDF → 建 paper + 后台解析 job；文件完全相同时返回 `dedup=exact_file` 复用 |
| GET | `/api/papers` | 列表（含 parsed / supersedes） |
| GET | `/api/papers/{id}` | 详情；解析后内容级查重命中时带 `dedup_alert`（官方用例 1） |
| GET | `/api/papers/{id}/docir?page=N` | DocIR JSON（blocks/toc/tables/figures/references） |

## 任务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/jobs/{id}` | 解析进度：status(queued/running/done/error) + progress(0~1) + message |

## 问答（SSE）

```http
POST /api/chat
Content-Type: application/json
{"question": "...", "paper_ids": ["p_xxx"]}   # paper_ids 缺省 = 全库
```

事件流：

```
event: retrieval   → {"channels": ["bm25"(,"dense")], "hits": [...]}        检索通道与命中
event: delta       → {"text": "增量token"}                                    仅生成式模式
event: final       → {"answer", "claims":[{text,status,support[]}],           溯源结果
                      "mode": "generative"|"extractive", "guardrails": [...]}
event: done        → {}
```

- 引用格式 `[^p页-§章节]`；引用后验校验失败会剔除无效锚点并记 guardrail
- `mode=extractive`：无可用 LLM 时降级为原文摘录（逐句仍挂锚点）

## 透明度 / 健康

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/audit/prompts?limit=50` | 调用日志（模块/模型/token/耗时/guardrail）+ 系统状态 |
| GET | `/api/health` | LLM 连通性（真实 ping）/ Dense 开关 / 论文数 |
