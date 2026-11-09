# Round4 验收清单（生产化补证：评测硬化 + 真实证据 + 边界补齐）· 2026-11-10

> 本轮针对评审 10 条意见（strict R@5 / rubric8 名实 / compare 浅 / OCR 证据 / anchor /
> copilot 真实性 / 性能并发 / 多用户 / 外部 API 鲁棒 / 文档一致性）逐项闭环。
> 证据全部由 `python scripts/accept_round.py --round round4-20261110 --min-tests 270`
> 及补跑脚本产出于本目录；如实呈现，不静默放宽门禁。

## 一、自动化门禁（quick，CI 同口径）

- [x] ruff —— All checks passed
- [x] pytest 全量 —— **274 passed / 0 failed**（本轮 +33：评测硬化 12 / 解析 round4 5 /
      推荐懒补+降级 2 / 角色扰动 1 / copilot 5 / 生产化 8）
- [x] P0 回归 8 条 · 官方 3 用例（容器等价）3/3
- [x] web tsc / lint / build —— 0 error（CI #15 success）
- [x] CI run #15 `completed success`（代码链 10 commit 已推送）

## 二、金标抗幻觉评测（eval_gold.json；Round4 门禁 16 项收紧后）

**终值（eval_gold.json @02:43）：12/17 门禁 PASS**——strict **0.9** / anchor **0.93（43 claims）** /
rubric 6/8（=0.75 达标）/ hallucination 0 / replay 10/10 / zh_top8 1.0 全绿；
**5 项 FAIL 如实呈现**（下轮目标，不静默放宽）：

| FAIL 门禁 | 值 | 根因分析 |
|---|---|---|
| gen_quote_unverified≤0.15 | 0.17 | 压线超 0.02（≈1 条 claim 引文转述未过 L2）——LLM 转述率波动 |
| compare_ok / missing_honest=1.0 | 0.0 | WMT14-BLEU 题：BERT 侧弱检索命中被 `_align_dimension` 填为 found，合成未如实声明「未报告」——**真实的对比抗幻觉缺口**（本轮金标暴露的核心发现） |
| compare_cell_quote≥0.8 | 0.42 | 部分 cell 的 quote 与其 block_ids 拼接覆盖 <0.85（chunk 跨块清洗差异） |
| compare_unsupported≤0.15 | 0.18 | 同一弱命中链路的连带 |

| 指标 | round3 | round4 |
|---|---|---|
| strict_recall_at5 | 0.6 FAIL | **0.9 PASS**（data-real 六篇验证口径 1.0） |
| rubric | 6 问冒名 8 | **真 8 问**（total==8 独立门禁，6/8=0.75 达标） |
| anchor_precision | 0.75/8 | **0.93/43**（分母=全部 verified support，口径收紧后） |
| anchor_claims≥15 | 8 | 43（生成题 5→12） |
| gen_quote_unverified | 无门禁 | 0.17（新进门禁，压线 FAIL 如实） |

- 首轮修复验证（data-real 六篇库，`--skip-llm`）：**strict_recall_at5 = 1.0（10/10）**，
  round3 四个失败模式全部修复：length→constant 变体 r1 命中；warmup（`_` 非 alnum 边界）；
  6（数字字母粘连拆分）；d_model=512（表块加权 ×2.5 后 r3 命中）。
- 首轮全量跑（20 篇库）暴露并修复：`pick_paper` 子串匹配误选 FlashAttention
  （金标题组整体失焦 → strict 0.2 / rubric 1/8 的假崩，非真实退化）——已改前缀精确匹配。
- LLM 非确定性在多轮 eval 间的真实波动已观察记录：rubric #7 依赖 compare 题①答案质量
  （检索弱轮会连带 FAIL）、#8 答案措辞需覆盖论文真实用语变体（已扩「无需额外参数更新」）。
- 运维发现（如实）：语料抓取当晚 embedding 配额 429 → dense 通道瞬断，中文查询
  （BM25 无法跨语言）依赖 dense 桥接——zh_top8/copilot 中文 idea 在 dense 断时如实降级。

## 三、Playwright e2e（10 用例）

- ① 双语滚动同步（定位器修复：最小含标题 card=真 pane，外层包装误中是旧 skip 根因）
- ② [n]→References · ③ CSV→图+图注 · ④ 多源搜索 · ⑤ G6 · ⑥ PDF↔译文 bbox
- ⑦ **反向联动**：点 PDF 块热点 → 译文定位闪高亮（PdfAnchored 透明热点，本轮新 UI）
- ⑧ **discover 含金量可逆过滤**：从库内导入 → 实质/背景/凑数徽章 → 隐藏凑数 → 卡数减少 → 恢复
- ⑨ **滚动同步量化：中线块锚定一致**（顶偏保持映射不变量：同块两栏视口内顶偏像素差 ≤80px；
  过程中修出真 bug——旧比例中心映射漂移 3 块 + 首版顶偏公式双计 offsetTop 实测 1264/600 过冲）
- ⑩ **拓扑四格式真渲染**：Mermaid svg 客户端渲染 + DOT/TikZ 源码 + 语法校验
- 最终结果：**10/10 passed（playwright.log，零跳过）**

## 四、模块证据（本目录产物）

| 证据 | 文件 | 结果 |
|---|---|---|
| 性能基准（LLM off 确定性口径） | bench.json | 全库建图 22 篇 **0.2s**；100k 行 CSV parse 0.46s+序列化 0.35s（JSON 合法）；**10 并发** reader 问答（抽取式降级）p95 5.7s **0 错**；PDF overlay 15 页论文含补遗 18 页导出 |
| 外部 API live | live_probe.json | arXiv 3/3 ✓ · OpenAlex 3/3（首轮 429→二轮全过）· S2 共享池 429（如实记录，多源冗余兜底）；15ms 超时对照 ConnectTimeout；**三网层全断优雅回放**（cache_offline 标注，无 live 冒充） |
| 20 篇图谱 | graph20.json | **23 节点/51 内部边/1624 外部引用边**；角色分化基石 8 · 边缘 1 · 空 14（阈值校准 PASS）；整批综述 5 节 42 claims |
| 综述/FutureWork 抽检 | audit_sampling.json · future_checks_review.md | **9/10=0.9 PASS**（口径：仅 verified+supported 计入分母；1 条 RoBERTa 位置编码对比 claim 锚定覆盖不足，如实列出） |
| 真实 LLM 写作 | copilot_live/framework.md | **PASS**：6/6 节实质（LLM 节尾如实标「证据不足，待补」不算否决）· References 白名单 0 编造 · [R#] 双向一致 + 5 项人工抽检清单（MANUAL_REVIEW.md） |
| PDF 导出视觉回归 | pdf_export_check.json + 12 张 PNG 样张 | Attention/GPT-3 两篇 ×overlay/bilingual：页数不减（15→18 含补遗）、**图片 3/3 不减少、矢量 drawing 2710/2710 全保留**、CJK 替换 2048 字符 |
| OCR 真实扫描件 | ocr_real.log / ocr_vlm.log | **3.2a VLM 通道 coverage=1.0、vlm_failed_pages=[]、fallback=None**（150 blocks/24 toc/5 公式/4 表格，756s）；3.2b UDL 541 页 MinerU 全书重解析超 3600s 上限如实中止（seg_002/11，docir 已还原快照，job 状态如实标 error） |
| 追踪 worker 真实刷新 | data-real/tracking_round4_evidence.txt | cs.CL 订阅：Sickle 子类目 Set 不存在→顶层 cs 超时→**Atom 兜底 20/20 条入库，abstract 800 字符/条**（元数据闭环生产验证） |
| L4 judge 抽检 | l4_judge_review.md | eval 最优 trace 的 claims 状态表（人工复核列留白） |

## 五、横切生产化

- **限流**：重端点 POST 令牌桶 30/min/匿名用户（429+Retry-After+X-RateLimit 头）；
  burst/跨用户独立/时间回填/进程内脚本豁免共 8 条测试
- **隔离**：双用户画像/推荐/反馈前后对照不变（画像=库词∪个人，库词共享是设计而非泄漏）
- **可观测**：X-Request-Id 回写+日志绑定；`GET /api/metrics`（请求/状态码/限流命中/
  LLM 聚合/任务/翻译/RSS）
- **安全**：上传 %PDF- 魔数+大小护栏（伪装 pdf→400）；sandbox 图路径穿越用例；CSV 公式注入（已有）
- **口径诚实**：单机单进程匿名多用户画像——**非多租户鉴权**（README/docs 如实注明）

## 六、过程发现的真 Bug（均已修复+回归锚定）

1. `pick_paper`/compare 子串撞名（FlashAttention/ALBERT/DeBERTa）→ 前缀精确匹配
1b. 双语滚动同步映射双 bug：比例中心映射漂移 → 顶偏保持；首版公式 `dst.offsetTop+rel`
    双计 offsetTop（等高栏 1264/600 过冲）→ `dst.offsetTop - (src.scrollTop - src.offsetTop)`
2. e2e pane 定位器命中外层包装 card（① 的历史 skip 与 ⑨ 的 11 块假偏差同根因）
3. mermaid 校验 and/or 优先级：任何含 `-->` 的字符串都过（+测试镜像同款 bug）
4. compare 金标 fabrication 正则误吞 `P2` 的 2（lookbehind 修复）
5. discover ⑧ 未切 zone tab；⑩ 说明文字撞 strict mode（heading 角色定位）
6. CSV GBK 解码回退链零测试；100k 行截断零测试（补齐）
7. tracked_items 无 abstract/citation_count → 推荐 citation 分量恒 0.5 中性（S2 懒补闭环）
8. Sickle 子类目（cs.CL）非法 OAI set → 顶层降试 + Atom 兜底（原直接失败）
9. 语料 20 篇后 refs 回归基线漂移（BART 34/Reformer 24/ELECTRA anomaly 11/59）→
   分层口径：六篇基线严格不变，新语料防退化下限，边界如实记录不硬卡

## 七、语料（data-real 快照 20 篇，references 健康）

Attention(40) · BERT(56) · GPT-3(118) · PreLN(101) · ScalingLaws(43) · UDL(245) —— 基线六篇
+ RoBERTa · ALBERT · ELECTRA · XLNet · BART · Longformer · BigBird · Reformer · DeBERTa ·
ViT · LoRA · FlashAttention · Megatron-LM · T5 —— 同领域 transformer 家族十四篇。
边界如实：Reformer 24 条 / BART 34 条（author-year 风格切分保守=宁少勿脏）、
ELECTRA anomaly 11/59（长条目占比）——留待切分调优，未放宽基线断言。

## 八、人工复核项（留白待填）

1. copilot_live/framework.md：抽 3 条 References 回白名单、2 段正文 [R#] 支撑性 —— [ ]
2. PDF 导出 12 张 PNG 样张：版式/图片/补遗页视觉核验 —— [ ]
3. future_checks_review.md：open_unverified/solved 边界 10 条 —— [ ]
4. l4_judge_review.md：误杀/漏杀 6 条 —— [ ]
5. 阅读器中→英方向切换实操（中文论文自动定向）—— [ ]
