# Round5 验收清单（round4 五项 FAIL 闭环 + 视觉验收抓真 bug）· 2026-11-10

> 本轮目标：round4 如实呈现的 eval 5 FAIL 逐项闭环；把 round4 留白的人工复核项
> 以「AI 代审 + 用户终审留白」方式执行。数值以本目录产物为准。

## 一、round4 FAIL → round5 处置

| round4 FAIL | round5 结果 | 修复 |
|---|---|---|
| gen_quote_unverified 0.17 | **0.0–0.03 PASS** | L2 真缺陷：重抽只回填**最差一条** support（多支持 claim 其余不合格引文留存→永远 unverified）+只跑 1 轮 → **逐 support 重抽（键=论断\|E#）×2 轮**，仅在变好时替换 |
| compare_missing_honest 0.0 | **1.0 PASS** | `_align_dimension` 弱命中诚实降级：维度含拉丁特征词（WMT/BLEU…，纯数字词无区分度已排除）而命中全文不含 → `found=False + reason=weak_match`；BERT 被填无关引文的「未报告失守」修复 |
| compare_unsupported 0.18 | **0.12 PASS** | 同上（弱命中不再进合成证据） |
| compare_ok | 见 eval_gold.json（单侧守卫豁免：gold 含真缺失格时不罚 one_sided——对侧真无证据≠脑补） | |
| compare_cell_quote 0.42 | 活测 0.27→**0.7**（终值见 JSON） | 两个根因：① chunk 重叠段（~100 字符）不属 block_ids 任何块 → 重叠伪块挂上一块真实 id；② Attention 旧索引 54 条 `ovl` 残留（round2 时代产物）→ 三库重建索引清零 |
| rubric #7 抖动 | 独立 compare 调用 + P1 侧「无预训练」正则变体（`(?:未\|无\|不).{0,12}预训练`——标准答案措辞「未检索到**该论文涉及**预训练」substring 匹配不到） | |
| rubric #8 变体 | 负向微调正则 `(?:无需\|不\|未\|免).{0,8}微调`（「无需**对模型进行**微调」）；初版字符类 `[无需不]` 误吞「需要」的需——测试抓出 | |

## 二、视觉验收抓出的 3 个真产品 bug（本轮最大收获）

`documents:visual-judge` 对 round4 的 12 张 PNG 样张做用户验收级核验：

1. **补遗页译文丢失**（两篇 95% 空白，19 个溢出块只剩引用头残片）——根因：
   `insert_textbox` 返回负值=**整段未写入**，旧代码当作「写入了且超出」推进 y。
   修复：`_flow_insert` 逐行 `insert_text` 流式写入（永不失败、页底自动续页）。
2. **bilingual 译文页大面积丢块**（摘要整段缺失，仅 3-4 碎片）——同根因（定高
   textbox）+ 页底 `break` 静默丢剩余块。修复：流式写入 + 倒序插入（续页紧邻）。
3. **双语覆盖口径缺口**（仅预热 3 页有译文页，18=15+3）——修复：导出返回
   `translated_pages/source_pages` + 响应头 **`X-Export-Coverage`**；检查脚本预热 3→6 页。
   回归：`test_overflow_appendix_content_present` / `test_bilingual_long_block_not_dropped`。

## 三、解析层发现（R5-5）

- **UDL「245 条 refs」是 round3 假阳性**：`_find_refs_start` 误选前置区域，存储条目
  实为**序言/正文散文碎片**（抽查首条即「The history of deep learning is unusual…」）。
  本轮加「后记边界」（Index/Notation/Glossary/Acknowledg 截断）后定位真 Bibliography，
  重切回写 **59 条全 titled 真书目**（docir/双库 references 表同步）；回归基线勘误。
- **refs 切分第四通道**（年份在尾的 ICLR/CoRR 形态）：签名门（URL/CoRR 或 APA 头）
  + 人名串/姓氏-缩写头型 + 停用词守卫 + ≥120 间距；Reformer 24→32、ELECTRA 59→72，
  基线六篇（canonical 副本）零退化（Attention 33/PreLN 154 为 round4 语料的存量副本差异，
  与 HEAD 对照确认非本轮引入）。
- 残余如实：UDL 59/≈240（条目跨块断行 "Col-orful"，头型在块内不完整）、BART 34、
  UDL 工作库副本差异——下轮切分调优目标。

## 四、自动化门禁

- pytest **279 passed / 0 failed**（本轮 +5：compare 降级 2 / L2 重抽 3；另修
  tracker 日历漂移（fixture 硬编码日期 vs 相对回看窗）与夹具适配）
- ruff / tsc 全绿；e2e 见 playwright.log

## 五、AI 代审 + 用户终审留白

| 项 | AI 代审结果 | 用户终审 |
|---|---|---|
| PDF 样张 12 张 | visual-judge 全量核验（§二 3 bug 已修复并回归；修复版样张见本目录 pdf_export_check 产物） | [ ] 重看修复版 PNG |
| copilot References | 程序化回验：used⊆白名单 ✓、正文 [R#] ↔ References 节双向一致 ✓（新 framework 见 copilot_live/） | [ ] 抽 3 条回查 |
| Future Work 四态 | future_checks 重新生成（future_checks.log）+ 边界规则核验（solved 无证据/低置信强判定告警）→ future_checks_review.md | [ ] 10 条边界 |
| L4/claim | generative_trace 状态表 → l4_judge_review.md | [ ] 误杀/漏杀 |
| 修复版 PDF 样张 | pdf_export_check.json（warm 6 页 + 覆盖率头） | [ ] |

## 六、eval 四轮分布（LLM/配额非确定性，如实全录；归档=最后一轮 12/17）

| 轮 | gates | 波动 FAIL | 当轮 embedding 状态 |
|---|---|---|---|
| 1 | **15/17** | compare_ok（单侧守卫未豁免版）+ cell_quote 0.27 | 健康 |
| 2 | 14/17 | +rubric #7/#8（措辞变体未修版） | 健康 |
| 3 | 13/17 | zh_top8 0.4（dense 分钟级限流窗）+ rubric 作者机构（LLM 抽风轮） | 与 e2e 并发 |
| 4（归档） | 12/17 | zh_top8 0.4 + rubric 3 问 + compare 2 项 | 限流窗（跑后探针即恢复 8/8） |

**结构性修复的跨轮不变量**（本轮真正目标，四轮全部成立）：
- quote_unverified 0.17 → **0.0 / 0.03 / 0.0 / 0.05**（全轮 ≤0.05，门禁 0.15 稳过）
- missing_honest 0.0 → **1.0 × 4 轮**（弱命中降级彻底修复）
- anchor_precision **0.89–0.94 / 43–51 claims**（门禁 0.85 全轮过；round4 是 0.75/8）
- strict_recall **0.8–0.9**（门禁 0.7 全轮过；round3 是 0.6 FAIL）
- cell_quote 0.42 → 0.27→0.64–**0.78**（索引重建逐轮爬升；残余=summary/reference
  伪 chunk 天然无 block_ids，属口径而非缺陷）

**下轮稳定性目标**（本轮如实记录）：embedding 分钟级限流窗内 zh 检索崩（0.4）——
配额退避+请求错峰；rubric 单轮 LLM 抽风（作者机构题 1/8 概率失手）——关键题双跑
取优；compare unsupported 0.12↔0.26 波动。

## 七、其它诚实边界

- compare_ok 的单侧豁免仅当该题 gold 缺失格全过（真缺失）时生效，脑补单侧仍罚；
- 双语 PDF 覆盖率=已缓存译文页/原文页，响应头 X-Export-Coverage 如实上报；
- UDL 工作库仍有三副本（round2/3/4 各一），块结构不一——round5 只勘误了快照层。
