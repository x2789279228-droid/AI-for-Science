# API 一览（MVP）

Base URL：`http://localhost:8100`（`API_PORT` 可改）

## 文献

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/papers` | multipart 上传 PDF → 建 paper + 后台解析 job；文件完全相同时返回 `dedup=exact_file` 复用（hash 查重先于落盘；插入+去重在锁内串行） |
| POST | `/api/papers/batch` | 批量上传（≤20 文件）：逐文件独立处理返回 `results[]`，单个失败不影响其他 |
| GET | `/api/papers` | 列表；默认只回 `status='active'`（软归档过滤），`?include_archived=1` 全量；命中带 `dedup_alerts[]`（双向 pending，含 score/breakdown/方向）与旧口径 `dedup_alert` |
| GET | `/api/papers/{id}` | 详情（authors/abstract/venue/year/doi/meta_source 结构化元数据；BLOB 字段不出参） |
| PATCH | `/api/papers/{id}` | 重命名（同步 DocIR meta.title + family 重算） |
| DELETE | `/api/papers/{id}` | 硬删除级联：文件/DocIR/chunks/FTS/refs/双向预警/相关 diff/jobs/translations |
| POST | `/api/papers/{id}/restore` | 归档恢复 active |
| POST | `/api/papers/{id}/reparse` | 重新解析（已有进行中任务时 409；旧 chunks/references 由解析流程自清） |
| POST | `/api/papers/{id}/dedup-decision` | 去重三态决策 `{decision: cover|keep|coexist, target_id}`；软归档语义（cover=旧 archived+superseded_by→新；keep=新 archived；coexist=双方 active+family 关联）；幂等，响应带 `diff_available` |
| POST | `/api/papers/{id}/version-diff` | DiffLens `{base_id}`：启动差异任务；已有缓存返回 `cached=true` |
| GET | `/api/papers/{id}/version-diff?base_id=` | 差异结果缓存（sections 对齐/段落 diff/数值变化/LLM 要点） |
| GET | `/api/papers/{id}/docir?page=N` | DocIR JSON（blocks/toc/tables/figures/references）；toc 条目含 `appendix` 标记 |
| GET | `/api/families` | 族视图（union-find 传递闭包）：`[{family_id, canonical_id/canonical_title/canonical_version, pending_alerts, versions:[{id,title,version,status,page_count,pending_alerts}]}]`；versions 按版本号升序，archived 成员保留（前端折叠展示）；族边=确定性同源（arXiv 基/DOI）∪ 人工已决策对，pending 分数对不入边（防错配经闭包传播） |

语义去重口径（9 层升级）：**blocking**（MinHash 32×4 LSH band ∪ family/arXiv 基/DOI 确定性
同源 ∪ 标题 token Jaccard≥0.6 ∪ 向量 cos≥0.85，候选外零特征成本）→ 加权精判（8 特征，
缺失按剩余权重归一化——缺失是「无证据」不是 0 分）：

```
score = Σ w_k·f_k / Σ w_k ，  w = {vector .30, title .20, author .10, minhash .20,
       rerank .10, ngram .05, containment .05, year .05}
```

- `vector`=标题+摘要 BGE-M3 余弦；`minhash`=正文 MinHash（boilerplate 滤除后）；
- `containment`=|A∩B|/min（版本演进 v1⊂v7 比 Jaccard 宽容）；`ngram`=字符 3-gram 余弦
  （摘要缺失时跳过，防与 title 同证据双计）；`rerank`=bge-reranker 成对精判（≤10 对/扫描）；
- **确定性同源（arXiv 基/DOI）直接抬到底线判定分 0.90**；
- **LLM 灰区复核**（默认开，`dedup_llm_judge`）：阈值下方一个 `dedup_gray_zone`(0.10) 带宽内
  的近失对、或过阈但正文指纹弱（minhash<0.5）的对，LLM 成对判断「同族/异族」并给理由——
  判同族+高置信 → 补警抬至阈值（breakdown 记 `llm` 分量）；判异族且无确定性同源 → 撤销预警；
  id 同源不受 veto，仅附注意见；
- `score ≥ dedup_threshold`（默认 0.85，见 `scripts/calibrate_dedup.py` 校准表：难负例
  P/R/F1 + 分量消融 + LR 学习权重对照）落 `dedup_alerts`，双向对称补警（TOCTOU 防线）；
  breakdown 分量键：`title/vector/author/minhash/containment/ngram/rerank/llm/year` +
  旗标 `same_arxiv_id/same_family/same_doi/reverse`。

## 任务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/jobs/{id}` | 解析进度：status(queued/running/cancel_requested/done/error/cancelled) + progress(0~1) + message |
| POST | `/api/jobs/{id}/cancel` | 协作式取消：阶段边界终止；MinerU 子进程 terminate；终态 `cancelled` |

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
