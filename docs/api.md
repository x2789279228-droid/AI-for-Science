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
| GET | `/api/papers/{id}/docir?page=N` | DocIR JSON（blocks/toc/tables/figures/references）；字段口径见下方「DocIR 细粒度字段」 |
| GET | `/api/papers/{id}/pdf` | 原始 PDF 文件流（阅读器 PDF.js 渲染 + bbox 高亮的取件源；容器内宿主路径缺失时按 uploads/{paper_id}.pdf 幂等回退） |
| GET | `/api/papers/{id}/figure/{figure_id}` | 图注关联裁图（MinerU 解析产物 image_path；路径校验必须落在解析输出目录内） |
| GET | `/api/papers/{id}/search?q=&top_k=10` | 全文检索（FTS5 chunk 级，CJK bigram）：`{results:[{chunk_id,page,section_path,snippet,score,block_ids}]}`——snippet 为首个命中词 ±60 字符窗口纯文本（前端 `<mark>` 自渲染，防注入）；`block_ids[0]` 供跨页块级跳转；q<2 → 400，未解析 → 404，无命中空列表 |
| GET | `/api/families` | 族视图（union-find 传递闭包）：`[{family_id, canonical_id/canonical_title/canonical_version, pending_alerts, versions:[{id,title,version,status,page_count,pending_alerts}]}]`；versions 按版本号升序，archived 成员保留（前端折叠展示）；族边=确定性同源（arXiv 基/DOI）∪ 人工已决策对，pending 分数对不入边（防错配经闭包传播） |

**DocIR 细粒度字段**（文本细粒度修复后的增量契约，只增不减）：

- `toc[]`：`{level(1-3), title, page(0基), id("s{n}"), parent_id, appendix?}`——`id/parent_id`
  由 `backfill_sections` 入口统一派生（书签/字号树/标题平铺三条路径共享）；
- `blocks[]`：`{id, type(heading|paragraph|formula), text, latex, page, bbox(归一化),
  section_path, section_id, appendix?}`；VLM 通道块级 bbox 来自 PROMPT v2 grounding 标记
  （`<<bbox:x0,y0,x1,y1>>` 0-1000），解析失败回退全页占位并标 `bbox_estimated: true`
  （meta 记 `vlm_bbox_mode: grounding|placeholder`、`vlm_prompt`）；
- `tables[]`：`{id, page, bbox, markdown, headers?, rows?, title?(表题), number?(表号)}`；
  跨页续表按「相邻页+同列数+归一表头相同+前表近页底/后表近页顶」自动合并；
- `meta.raw_orders`（模块三）：`{"<0基页码>": [perm]}`——PyMuPDF 阅读顺序重排的真实置换
  （`perm[k]`=原始序第 k 块在重排后数组中的索引，仅记真正发生重排的页）；前端阅读顺序
  可视化三级还原：demo 素材 → 解析记录（红，真实）→ x0 启发（黄，标「模拟」）；
- `meta.quality`：`{text_coverage, toc_depth, heading_rate(toc→块命中率,剥编号口径),
  title_ok, formula_count, table_count, reference_parse_rate(title≥8字才计),
  scanned_ratio, overall}`——overall 权重：覆盖率 .35/层级 .10/heading 命中 .15/
  结构化 .20/文献 .10/title_ok .10；
- 元数据链：arXiv Atom（含 title）→ 首页正则/合成 DOI `10.48550/arXiv.<id>` →
  **Crossref 按题名兜底**（相似度 ≥0.6，meta_source 追加 `crossref`）→ LLM → 启发式
  （摘要支持 `Abstract—` 行内形式与书籍前言/Preface 首段）；stem 退化标题
  （`p_xxx/seg_xxx/fx_*`）由权威层 title 回填。

**抗幻觉修订口径**（verify.py 逐字修订）：

- **L2 逐 support**：每个 support 的 quote 对**它自己引用的证据**算覆盖率，全部 ≥0.85 才
  `verified`；claim 带 `quote_coverages[]` 数组，重抽回填到覆盖率最低的 support
  （旧实现只校验 support[0]，多支持时其余引文从不被检查）；
- **L4 unchecked**：`semantic_judge` 关闭 / LLM 不可用 / 判定失败 → 候选 claim 一律
  `semantic_status="unchecked"`（前端「语义未核」灰徽章）+ guardrail
  `semantic_judge_off`/`semantic_judge_skipped`——绿色「已验证」自此仅代表 L2/L3 通过；
- **support 原句块锚点**：responder 对每个 support 的 quote 在其引用 chunk 的 DocIR 块中
  定位来源块（归一化包含），命中写 `support.block_id / block_bbox`——前端红框优先块级
  （claim 原句级），缺失回退 chunk 级 E# 锚点；未命中不写字段（诚实）；
- **compare_table 格级**：cell 增 `eid`（Evidence Pack 内 E#，点击直通原文）与行级
  `conflict`（同维度 ≥2 格数字集合两两不相交 → ⚠ 冲突徽章，系统不裁决）；三条新
  guardrail：`compare_coverage_low:{f}/{t}`（命中率<50%）、`compare_conflict:{n}`、
  `compare_one_sided:{c}/{n}`（叙述 supports 未覆盖全部选文）；
- **评测口径**（eval_gold）：Recall@5 宽口径（gate）+ `strict_recall_at5`/`mrr_at5`（报告）；
  `anchor_precision`（claim 首 support 原句块定位且 quote 归一包含，≥0.7 gate，空 claims
  不再伪装 True）；**rubric8**（方案 §2.2 官方考点 8 问：作者机构/β1β2/GPU/BLEU/论证链/
  [11] 动态引用，≥0.75 gate）；**compare_ok**（v7+BERT 双篇 forced compare 端到端：表格
  rows≥2、覆盖≥0.5、格级 eid 覆盖≥0.6、supported≥0.8、P1/P2 标签、无单侧告警）；
  对比语料由 `scripts/fetch_corpus.py` 收录（BERT/PreLN/GPT-2 arXiv 三篇）+ `--snapshot`
  固化进 data-real（含 UDL）。

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
{"question": "...", "paper_ids": ["p_xxx"], "agent": "auto", "conversation_id": ""}
# paper_ids 缺省 = 全库 active（>8 篇截断为前 8 并发 guardrail:paper_cap 事件）
# agent: auto|reader|compare|graph|writer|viz（UI 强制指定 Agent）
```

事件流（协议 v1.1，向后兼容；`trace` 起为节点卡时间线，`plan/tool_call/tool_result/evidence/verifier` 为 Trace 增量）：

```
event: trace*      → {"node": "...", "status": "live|done", ...}              节点卡片（live）
event: plan        → {"steps": [...]}                                         Planner 检索面规划
event: tool_call / tool_result → {...}                                        工具调用时间线
event: retrieval   → {"channels": ["bm25"(,"dense")], "hits": [...]}          检索通道与命中
event: evidence    → {...}                                                    Evidence Pack [E#] 编号下发
event: delta       → {"text": "增量token"}                                    仅生成式模式
event: verifier    → {"claims": [...]}                                        L1-L4 校验过程
event: artifact    → {"type": "compare_table"|"chart_spec"|"citation_graph"|"outline", ...}
                                                                              Agent 专属产物
event: guardrail   → {"message", "kind"}                                      护栏事件（选文超 8 篇/单侧覆盖等）
event: final       → {"answer", "claims":[{text,status,support[],anchors[]}],  溯源结果（ClaimTrace 三态
                      "mode": "generative"|"extractive", "guardrails": [...],   supported/needs_check/unsupported；
                      "trace": [...], "anchors": {...}}                         anchors: E#→{page(1基),bbox,block_ids}}
event: done        → {}
```

- 引用格式 **`[E#]`**（Evidence Pack 显式编号，多论文自动分配 P1/P2 代号）；引用后验校验失败会剔除无效锚点并记 guardrail
- `mode=extractive`：无可用 LLM 时降级为原文摘录（逐句仍挂锚点）
- 断点续跑：图级异常标记 thread failed，`POST /api/chat/{thread_id}/resume` 从 checkpoint 续跑（SSE 同协议）

## 会话线程 / Trace 回放

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/threads?conversation_id=` | 会话线程列表（chat_threads 账本，status: running/done/failed） |
| GET | `/api/threads/{thread_id}` | 线程详情（question/answer/error/resume_attempts） |
| POST | `/api/chat/{thread_id}/resume` | 断点续跑：从最近 checkpoint 续跑（SSE 事件流同 /api/chat） |
| GET | `/api/qa/{trace_id}` | 回放一次问答的完整事件流（trace_id = thread_id；trace_events 落盘，含断点续跑并入的事件） |

## 透明度 / 健康

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/audit/prompts?limit=50` | 调用日志（模块/模型/token/耗时/guardrail）+ 系统状态 |
| GET | `/api/health` | LLM 连通性（真实 ping）/ Dense 开关 / 论文数 |

## 翻译与导出（模块三）

- `POST /api/papers/{id}/translate` — SSE 块级翻译。body：`{lang?, target_lang?: zh|en, source_lang?: auto|zh|en, mode?: academic|literal, page?, section?, block_ids?, include_extras?}`（include_extras=表格 markdown+图注；缓存键 `target_lang|mode`，legacy `zh` 行兼容命中）
- `GET /api/papers/{id}/translation?lang=&mode=` — 缓存译文（复合键优先，zh|academic 回退 legacy）
- `POST /api/translate/selection` — 划词翻译 `{text, context?, target_lang?, mode?}`
- `GET /api/papers/{id}/export?fmt=pdf|md|html&target_lang=&mode=&layout=overlay|bilingual&view=mono|bilingual` — 译文导出（PDF 原位替换/双语交替；响应头 X-Export-Pages/Replaced/Overflow）
- `GET|POST /api/terminology`、`DELETE /api/terminology/{term}` — 术语表 CRUD
- `GET /api/terminology/export`、`POST /api/terminology/import` — 术语表 CSV 导入导出

## 检索 / 追踪 / 推荐（模块四）

- `POST /api/discover` — 方向一句话 SSE 自动闭环：LLM 抽英文检索词 → arXiv 检索 →（`auto:true` 时）自动下载 top1 → 等解析 → hybrid_search 出证据；事件 `status/discover_results/final/error/done`
- `POST /api/discover/search` — 多源检索 SSE。入口四选一：`{query}`（arXiv+S2+OpenAlex 去重）/ `{title|doi|arxiv_id}`（精确解析）/ `{references_text}`（BibTeX/RIS/纯文本 → S2 batch 补全）
- `POST /api/discover/download` — `{arxiv_id}` 或 `{pdf_url}` 二选一下载入库（`%PDF-` 魔数校验，arxiv 主源 + export 备源）
- `GET /api/discover/repro?repo=` — GitHub 复现性懒加载（stars 档位/活跃度/许可证 → 复现分）
- `GET /api/sandbox/figure/{name}` — matplotlib 沙箱产物（文件名白名单防路径穿越）
- `GET /api/papers/{id}/ref-quality` — 参考文献含金量（实质/背景/凑数 + 引用句明细；无引用句条目不出现=中性态）
- `GET|POST /api/tracking/subscriptions`、`DELETE /api/tracking/subscriptions/{id}` — 订阅 CRUD（keyword/author/category）
- `POST /api/tracking/refresh` — 手动刷新（Sickle OAI 主通道/Atom 回退；返回通道与新增/去重计数）
- `GET /api/tracking/items?sub_id=&status=`、`POST /api/tracking/items/{id}/dismiss` — 追踪条目
- `GET /api/recommendations?refresh=`（头 `X-User-Id` 多用户隔离）— 推荐流（五分量打分+理由）
- `POST /api/recommendations/{rec_id}/feedback` — `{vote: 1|-1}` 赞踩（重加权画像）

## 图谱 / 综述 / 前沿（模块五）

- `GET /api/graph?paper_ids=a,b,c` — 批量建图（缺省全库；含金量加权边+角色+社区）
- `POST /api/graph/survey` — `{paper_ids, refresh?}` 整批领域综述 SSE（五节结构；survey_reports 缓存；<2 篇 400）
- `POST /api/future-checks` — `{paper_ids}` 外部最新进展核查 SSE（S2 证据；verdict 含 open_unverified；结果落 future_checks）
- `POST /api/graph/summary`、`POST /api/ideas` — 社区综述 / 库内 Future Work（原有）

## Copilot（模块六）

- `POST /api/copilot/writing` — `{idea, paper_ids, sections?}` 无幻觉论文框架 SSE（References 白名单后验；guardrails 审计；结果落 writing_drafts）
- `POST /api/copilot/datasets`（multipart file）— CSV 上传（≤5MB/10 万行/编码嗅探/公式注入清洗/类型推断）
- `GET /api/copilot/datasets`、`GET /api/copilot/datasets/{id}` — 列表与预览（前 50 行）
- `POST /api/copilot/chart` — `{dataset_id, chart_type: table|line|bar|radar|scatter3d, x?, y?..., title?, with_caption?}` 五类图 spec（scatter3d 真 3D 散点：≥3 数值维 + 第 4 维 visualMap，echarts-gl 渲染）+ Figure Caption（数字可回溯）
- `POST /api/copilot/diagram` — `{description}` DiagramSpec→Mermaid/DOT/TikZ 源码 + matplotlib PNG + 各格式语法校验
- `POST /api/copilot/translate` — `{text}` 或 `{paper_id, scope}` 中→英学术翻译
