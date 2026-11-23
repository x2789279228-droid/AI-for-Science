# Meridian — 科研助手 Agent

> 传智杯「AI 大模型创新应用挑战赛」参赛作品 —— PDF 深度解析 + 抗幻觉溯源问答 + 中英联动阅读 + arXiv 检索推荐 + 引用图谱挖掘 + 学术写作 Copilot，六大模块共用一套 **DocIR** 数据底座；v2 新增 **模块七「时间感知记忆与研究状态演化」**（记忆事件流 / 时间知识图谱 / 假设 FSM 三层分离，详见 [docs/模块七-实现说明.md](docs/模块七-实现说明.md)）。

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
uv run uvicorn app.main:app --reload --port 8100
```

- 健康检查：`GET http://localhost:8100/api/health`（LLM 连通性真实 ping 检测）
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

## 当前模块状态（模块三~六已按赛题要求补齐；下列 ✅ = 端到端可用且带自动化测试）

| 模块 | 状态 | 说明 |
|---|---|---|
| 一 PDF 解析 | ✅ | **解析路由**：probe→native(MinerU 2.5.4)/scanned(Qwen-VL 逐页)/gray(逐页 VLM 救援)/long(50 页/批+前 120 页索引)；失败→PyMuPDF 兜底；公式 LaTeX→KaTeX；**References 三通道切分**（行内 [n] / 字母键 [ABC+18] / author-year 作者头 + 标题粘连兜底，Appendix 边界截断；authors/year/venue/title_norm 富字段落库）——BERT 56 / PreLN 101 / **GPT-3 118** / ScalingLaws 43 / **UDL 991** 条（round6 重解析；断行碎片/41 anomaly 如实标注），零脏巨块；`ref_count_consistency` 质量探针 |
| 二 溯源问答 | ✅ | BM25(中文 bigram)+可选 Dense→RRF→Rerank；Evidence Pack `[E#]`；L1-L4 Claim-Citation 校验；ClaimTrace 四态；引用点击跳 PDF bbox；SSE 流式 |
| 三 阅读与双向翻译 | ✅ | 五视图（PDF/文本/双语对照/原版对照/**PDF↔译文联动**）；**[n]↔References 双向无损跳转**（含跨页回退）；双语 IntersectionObserver 防回环同步；**双向翻译**：英→中 + 中→英学术体（target_lang/mode，复合缓存键 `zh\|academic`，legacy 缓存兼容命中）；划词/悬停（可开悬停即译）/整篇（含表格+图注）翻译；**导出**：MD/HTML（对照/单栏）+ **PDF 原位替换 overlay**（redaction 保图保矢量、公式不动、溢出进补遗页）+ 双语交替版；术语表 CRUD+导入导出 |
| 四 检索追踪推荐 | ✅ | **多源检索**（arXiv Atom + Semantic Scholar + OpenAlex，DOI→arXiv→title_norm 三索引去重）；**参考文献列表下钻**（BibTeX/RIS/纯文本→S2 batch 补全，未解析如实标注）；**引用含金量**（实质/背景/凑数；正文引用支持数字 `[n]`、分组 `[n, m]`、字母键 `[ABC+18]`、author-year 四种样式；默认自动剔除凑数、可逆；附 `coverage` 口径）→ References 面板可逆过滤；**arXiv 追踪**（Sickle OAI-PMH 主通道+Atom 回退、订阅表、后台 worker、last_run 水位线续跑、库内已知标记）；**个性化推荐**（本地库画像打分 local/recency/citation/github/keyword 五分量+可解释理由，👍👇 反馈重加权，X-User-Id 多用户隔离）；`/discover` 四区 UI |
| 五 图谱与综述 | ✅ | Louvain 社区+PageRank+介数（networkx 介数 + scipy 缺失时纯 Python PageRank 回退，backend 如实标注）+基石🪨/边缘衍生🌿+含金量加权边；G6 交互图谱 + **批量选择 5–20 篇** + PNG/JSON 导出；**整批领域综述**（五节结构：背景/时间线/方法族/基石边缘/开放问题，每节 [E#]+ClaimTrace，survey_reports 缓存）；**Future Work 外部核查**（尾章 heading 抽取→CJK 语义去重→S2/OpenAlex/arXiv 三源证据→solved/partial/open/**open_unverified** 诚实态+置信度+证据链；LLM 离线时规则初判标 `verdict_source=rule`）；/ideas 两栏 |
| 六 写作与可视化 Copilot | ✅ | `/copilot` 四 Tab：**① 无幻觉论文框架**（固定章节 Abstract→…→References；References 白名单=选中论文 references+库内条目按篇配额（每篇≤20/总≤60，每篇保底 5），正文 [R#] 白名单外自动剔除；used<3 时一次补引重试，仍不足如实 `refs_underused`；导出只列被引条目，未引用单列不编号；guardrails 审计）**② CSV 可视化**（≤5MB/10 万行护栏+编码嗅探+公式注入清洗+按行预算截断防烂 JSON；table/line/bar/radar（多维，轴语义修正）/radar3d（3D 透视层叠，如实标注 ECharts 无原生 radar3D）/scatter3d 真 3D 散点；bar 类别>60 时 Top60+其他聚合且图注仍用全量统计；Figure Caption 数字全部可回溯数据集，LLM 润色数字越界自动回退模板）**③ 中→英学术翻译**（复用模块三管线）**④ 拓扑图脚本**（LLM 只产 DiagramSpec→确定性渲染 Mermaid/Graphviz DOT/TikZ + matplotlib sandbox PNG；LLM 不可用时规则图降级 `guardrail=diagram_rule_fallback`；幽灵边剔除+语法校验） |
| 翻译底座 | ✅ | 占位符保护（公式/代码/[n]）+术语表优先+缓存续译+2 并发限流；SSE；导出审计 translation_exports |
| 横切生产化（Round4） | ✅ | 重端点 POST 令牌桶限流（30/min/匿名用户，429+Retry-After）· X-Request-Id · `GET /api/metrics`（请求/LLM 聚合/进程资源）· 上传 %PDF- 魔数护栏 · 匿名多用户画像隔离（**非多租户鉴权，如实口径**）· bench 性能基准（建图 0.2s/100k CSV 0.8s/10 并发 p95 5.7s 0 错） |
| Prompt 审计 | ✅ | `app/prompts/*.yaml` 版本化（translate 1.1.0 / insights 1.1.0 / copilot 1.0.0 新增）；qa_logs.prompt_version；`/audit` 版本分布 |

### 诚实边界（如实标注，不冒充）

- PDF overlay 导出**保留原表格**（避免破坏版式），表格译文进 MD/HTML 与双语文本视图；
- S2/OpenAlex 无 key 有共享限速：适配器缓存+退避，断网时 `source=cache_offline` 如实回放；
- Future Work 外部核查无证据时标 `open_unverified`（≠确认没人做）；LLM 离线时 `partial` 为**规则初判**（`verdict_source=rule`、confidence 0.4），不当强结论；引用含金量只对能匹配到 references 的引用句计数，未匹配保持中性态（不硬猜）；正文引用四种样式（数字/分组/字母键/author-year），字母键只映射 raw 开头带键的条目；
- Copilot 「3D 雷达」为 **ECharts 2D radar + 前端 3D 透视层叠**（ECharts 无原生 radar3D，UI/文档如实标注）；真 3D 数据用 scatter3d；
- LLM 不可用时：综述出图谱客观数据骨架、写作节标「待补：证据不足」、图注回退确定性模板、拓扑图走规则降级——均不冒充生成；
- 限流/隔离是**单机单进程匿名多用户画像**口径（X-User-Id 分桶），非多租户鉴权/分布式治理；
- 中文查询→英文语料的检索依赖 Dense 多语向量桥接（BM25 无法跨语言）——embedding 配额限速时如实降级（Round4 实测记录于 docs/acceptance/round4-20261110/）。

## 核心机制（对应参赛方案评分点）

| 机制 | 说明 |
|---|---|
| **DocIR 统一底座** | PyMuPDF 解析 → blocks(page,bbox 锚点) / 标题树 / 表格 / 图注 / References，六大模块共用 |
| **混合检索** | SQLite FTS5(BM25，CJK bigram 中文可命中) + 可选 Dense(OpenAI 兼容 embeddings) → RRF(k=60) → bge-reranker 全局精排（失败静默降级融合排序 + guardrail），表格单独成块支撑数值类问题 |
| **四层防御** | ① 混合检索+Rerank（SiliconFlow bge-reranker-v2-m3，未配 RERANK_API_KEY 时降级 RRF 融合排序+guardrail 如实标注） ② Evidence Pack（块级 page/bbox/章节，显式编号 E1..En）③ 强制 `[E#]` 引用生成（无效编号剔除+定向重生成环）④ **Claim-Citation 校验器**：L1 编号存在性 / L2 引文原文覆盖率≥0.85（失败重抽）/ L3 数值·型号逐字比对（未命中标「需核对」）/ L4 语义 judge（LLM 批量判定，no→未验证，失败兜底跳过） |
| **ClaimTrace 四态** | 已验证（绿）/ 需核对（黄：数值或引文待人工核对）/ 未验证（红）/ 缺失声明 absent（诚实负向，不计失败）；`final.anchors` 携带 E#→块级锚点，前端点击弹 PDF 对应页 + bbox 红框高亮 |
| **拒答策略** | 查无依据明确拒答并附「最接近的 3 条证据」；寒暄/范围外拒答；无 LLM 抽取式降级（停用词过滤防噪声命中） |
| **页码口径** | SSE 对外统一 1 基人读页码；DB/DocIR 内部 0 基 |
| **语义去重** | 文件 hash 精确去重 + 标题内容级匹配（官方用例：先 v7 后 v1 触发预警） |
| **后台任务** | 解析走线程队列 + 进度条，541 页超长文档不阻塞 |
| **透明度中心** | `/api/audit/prompts`：每次 LLM 调用的 token/耗时/guardrail 事件全落库 |

## 前端改造方案 v1.0（2026-11-11 落地）

> 方案全文：`docs/前端改造方案.md`；视觉验收截图：`docs/acceptance/round1/screenshots/`（7 张，1440×900）。

- **C1 品牌层收敛**：经典浅蓝=唯一工作皮肤，RISO 降级品牌层（intro/印章/`?theme=riso` 截图通道）；印章语义（证/基）tokens；字体语义三轨（--font-display-cn/en/--font-data）+ `--text-h1=26px`；排期徽章全下线；ClaimTrace 四态色收敛
- **C2 研究工作台**：首页 = 数据带（运行时取数：篇数 / 库内引用边 / 外部引用追踪 / LLM 状态）+ 最近问答卡（`GET /api/qa/recent` 唯一后端新增）+ 图谱只读缩略卡 + 文献库分区
- **C3 Chat 演示入口**：空态演示问题卡（按选中论文动态，点击即发送）；claim「证」章 Stamp；E# mono 脉冲；回答 15px/68ch
- **C4 图谱舞台化**：画布 ≥560px（实测 664）；标签词典零截断；社区 Hull 凸包（双降级路径）；基石「基」章 badges；图例；统计 popover；详情抽屉 340px+去问答
- **C8 健康中心(/health,导航第 10 项,2026-11-15 重写)**:结果健康评分卡——**7 个固定权重运行时子分**(可靠性 20%/回答可信 20%/回答覆盖 5%/检索质量 15%/解析质量 15%/性能 15%/数据卫生 10%),全部由最近 7 天**本机运行轨迹**计算(排除 kappa_/gold_/bench_/e2e_ 等评测/测试前缀;样本 <30 条不出总分);失败 trace/任务进可靠性分母、拒答/无答案单列回答覆盖,缺失维度按 0 计**不重归一化**;性能用窗口 p50/p95 SLO 而非均值;最新离线金标 gates 未全过时总分**封顶 90**（eval-20261115-r1 全过后当前不触发封顶）;返回 n/Wilson 95%CI/公式/原始计数;judge 双 κ 校准（judge-only 0.856 / pipeline 0.928,n=28,bootstrap 95%CI,构造负类口径如实声明）与金标轮次趋势同页披露。工程快照(pytest/e2e,来自 `web/lib/evidence.ts`,由 `scripts/write_evidence.py` 自动生成)单独展示、**不计入健康分**;另含 ClaimTrace 四态/去重三态/外部校准明细;后端唯一增量 `GET /api/health-report` 只读聚合
- **C5/C6 一致性**：阅读器五视图分段控件+⋯菜单；EmptyState 空态规范；Ideas 栏头色块；Discover 示例词点击即搜；/judge 评审导览（8 步路线+工程证据）；Placeholder/排期徽章全下线
- **门禁（2026-11-15 实跑，详见 `docs/acceptance/latest-verify/`）**：ruff 0 error · tsc 绿 · build 绿 · pytest **342 passed** · Playwright e2e **19 条**（含 TOC 跨页/分组引用/大纲错误态/TextLayer scale/图谱含金量 chip 五条新回归；全部离线可跑，无 LLM 依赖）
- **Round6-P1 检索与校准加固（2026-11-15）**：dense 向量回填脚本（`scripts/backfill_embeddings.py`，实测修复 11/21 篇 vec 全缺的静默损伤——BM25 对纯中文查询零召回，dense 是中文检索唯一通道；健康页披露 `dense_vec_coverage`，当前 100%）；版权/许可样板块降级（检索尾部重排 + compare 格选让位）；`llm.chat` 瞬时故障重试（429/5xx/网络，1s/3s 退避——planner 不再单次 429 即掉规则兜底）；judge 提示词数值/实体严格化 + `semantic_status` 全量记录，构造真值双 κ 校准 **judge-only 0.856 / 全管线 0.928**（n=28，bootstrap 95%CI，健康页披露 + 「非独立人工标注」口径声明）；compare 金标评测 3 题并行化（每题耗时入证据）；`qa_logs.doc_mode` 每模式 SLO（健康页延迟分列）；`/api/chat` 空问题 400（不产生假中断轨迹）、可恢复中断与健康失败分列披露
- **已知边界**：双语同步指示线（§6.2）、统一 Tooltip（§8.1）、快捷键 cheatsheet（§8.3）为可选项未实施；节点标签 canvas 文本不可 DOM 断言（⑬ 由视觉截图人工核）

## 证据与可复现（自动生成）

> 页面上所有测试/评测/语料数字不再手写：先跑门禁把日志落到 `docs/acceptance/latest-verify/`，
> 再执行 `cd api && uv run python scripts/write_evidence.py` 生成
> `web/lib/evidence.ts`（`/judge`、`/health` 直接 import）与
> `docs/acceptance/latest-verify/summary.md`。当前快照：见该 summary。

## 测试

```bash
cd api && uv run pytest -q          # 后端全量：单元 + API + 进程内 E2E（342 用例，HTTP 全 mock；含 qa/recent 轻接口 3 例）
cd web && pnpm e2e                  # Playwright UI 验收 19 条：双语滚动同步 / [n]→References /
                                    #   CSV 出图+图注 / 多源搜索 / G6 / PDF↔译文 bbox 双向（⑦反向）/ 
                                    #   discover 含金量可逆过滤 / 滚动同步中线块锚定量化 / 拓扑四格式渲染
                                    #   + 首页工作台 / Chat 空态演示卡 / 图谱舞台 / 评审导览 / 健康中心
                                    #   + 2026-11-15 新增：标题树跨页定位 / [n,m] 分组引用 / 大纲失败重试 / TextLayer scale / 图谱含金量 chip
# 抗幻觉金标评测（需真实 LLM）：scripts/eval_gold.py --out ../docs/acceptance/<round>/
#   17 项门禁（anchor≥0.85 全 verified support 口径 / rubric 真 8 问 /
#   compare 3 题手工金标矩阵 / quote_unverified≤0.15）——最新见 docs/acceptance/eval-20261115-r1/
#   （17/17 gates 全过：hall 0.02 / anchor 0.93 / compare 3/3 gold_cell_acc 1.0 / replay 10/10；
#    历史不过项如实归档不删——eval-20261114-r3 16/17 留档）
# 一键验收：python scripts/accept_round.py --round round4-<date>（quick 门禁 + 全套真实证据链）
# 数据快照：scripts/fetch_corpus.py --snapshot（19 篇 arXiv + UDL = 20 篇同领域语料固化 data-real；
#   官方去重演示口径：快照只含 v7，上传 v1 即触发语义去重预警；2026-11-15 重固化：chunks 向量 100% 覆盖）
# dense 向量回填：scripts/backfill_embeddings.py（幂等，只补 vec IS NULL；入库时 embedding 失败的
#   论文 dense 通道会静默缺席——中文检索劣化的隐形根因，健康页 dense_vec_coverage 可监控）
```

### 语料（data-real 快照 20 篇同领域，references 健康）

基线六篇：Attention(40) · BERT(56) · **GPT-3(118)** · PreLN(101) · ScalingLaws(43) · UDL(**991**，541 页 MinerU 全书重解析+跑页头簇首定位)；
Round4 扩充十四篇 transformer 家族：RoBERTa · ALBERT · ELECTRA · XLNet · BART · Longformer ·
BigBird · Reformer · DeBERTa · ViT · LoRA · FlashAttention · Megatron-LM · T5。
边界如实：Reformer(24)/BART(34) 条 refs（解析层块内无边界标记，BART 待重解析）、ELECTRA anomaly 11/59、UDL anomaly 41/991=4%。
勘误：2001.08361 是 Kaplan 缩放律（旧脚本误标 "GPT2"）；UDL 旧基线 "245/59 条" 为序言散文假阳性（round6 已全书重解析为 991 条真书目，41 条 anomaly 如实保留）；GPT 系原文以 GPT-3 论文代表
（GPT-2 原文无 arXiv 版，OpenAI 技术报告 PDF 不在 arXiv 通道——如实注明）。

## 快速开始

```bash
# 本地开发（Windows / Git Bash）
cd scholar-agent
make install          # api: uv sync · web: pnpm install（含 sickle/bibtexparser 可选依赖）
cp .env.example .env  # 填 LLM/EMBEDDING key（无 key 也可跑：抽取式降级闭环）
make api              # 终端 1：API :8100（启动即挂 arXiv 追踪 worker，间隔 TRACKING_INTERVAL_S）
make web              # 终端 2：前端 dev :3005（:3000 留给 docker demo；本机如占用可改）
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

## 团队信息

**谢仁帆**（个人参赛 · B 组 · 参赛编号 2131172）

- 所属院校：湖南科技大学 · 信息安全专业 · 本科 2024 级
- 联系电话：13026852707
- 联系邮箱：2789279228@qq.com
- 指导老师：文吉刚（初赛 / 省赛 / 国赛）

## License 与致谢

- 本项目基于 [MIT License](LICENSE) 开源。
- 感谢以下开源项目与云服务生态：MinerU（PDF 解析）、fastembed / BGE-M3（本地向量）、
  networkx（图谱算法）、AntV G6 / ECharts（可视化）、FastAPI、Next.js，
  以及火山方舟（doubao 系列模型与 Embedding 服务）。
