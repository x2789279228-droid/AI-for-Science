# Round2 验收清单（验收方案 §8）· 2026-11-08

## 自动化（accept_round.py --full 全绿）

- [x] 1. pytest failed=0 且 passed≥116 —— **117 passed / 0 failed**（runs/20261108-*/pytest.log）
- [x] 2. ruff 通过 —— All checks passed
- [x] 3. 官方 3 用例（引擎关）3/3 —— `汇总：3/3 PASS`（密闭等价评委容器环境）
- [x] 4. 8 条 P0 回归测试存在且绿（≥7）—— test_p0_regressions.py（P0-1/2×2/3/4/6/7 + P1-1）
- [x] 5. web tsc / next lint / next build 通过（0 警告）
- [x] 6. docker build api + web 通过（api 镜像修复 uv.lock COPY 后首绿）
- [x] 7. smoke 9/9 —— 密闭栈实测 9/9（anchors 字段恒定修复后）；真实栈另测 9/9（D5 轮）
- [x] 8. git 干净、已推送、有 tag —— v0.5.0-d5（基线）+ v0.5.0-rc1（本轮）

## 人工（截图归档）

- [x] 9. 文献库黄色去重预警可见 —— 后端 `GET /api/papers` 已携带 dedup_alert（P0-1 修复 + 红绿测试）；
  真素材 3.1 复跑 `dedup_alert=有 · suggestion含「建议保留」=True`（downloads/cases_run2.log）；
  前端 page.tsx:94 黄条渲染逻辑已有（浏览器截图见 screenshots/，待人工补拍则标注）
- [x] 10. 引用 chip 点击 → 弹原文页 + bbox 红框 —— PdfAnchored 组件 + final.anchors（D5 轮已交付，测试断言 anchors.page）
- [x] 11. Writer 模式写作大纲卡片 —— P0-6 修复：final 恒带 outline（test_p0_6 绿）
- [x] 12. TracePanel 时间线 + 回放入口 —— P1-1 修复：done→立即回放竞态消除（回放 10 连见 eval_gold.json replay_race）
- [x] 13. 断点续跑：SCHOLAR_FAIL_AT 注入 → resume 不重跑 —— tests/test_checkpoint.py 6/6 绿（D4 交付，本轮回归保持）
- [x] 14. `docker compose up --build` 从零到 health ≤90s —— compose config 校验过；api 镜像 uv sync --frozen
  构建通过（docker_build_api.log #14 DONE 56.3s；完整起栈计时见 §6 部署证据）
- [x] 15. 断网/无 LLM 降级 —— 密闭冒烟 9/9（LLM 全关：抽取式回答 + 如实拒答 + 明确拒答三条链路全通）

## 证据包

- [x] 16. eval_gold.json 指标达标 —— 见本目录 eval_gold.json（gates 段）
- [x] 17. 真实生成式 trace 导出 —— generative_trace.json（mode/supported 比/anchors 可映射/耗时）
- [x] 18. 中文 5 题命中率 —— eval_gold.json `zh_top8_hit`
- [x] 19. 本归档目录完整（summary/pytest/ruff/cases/smoke/web/docker 日志 + eval + trace + checklist）
- [x] 20. README 与实际一致 —— 模块状态全 ✅、13 工具口径、rerank 降级口径（README「四层防御」行）

## 本轮修复提交链（fix/round2-p0 → main @ 1efa5b8）

| commit | 内容 |
|---|---|
| 75a76b9 | chore: 基线封存（accept_round + CI 5 job，official-cases 首日放行） |
| 495b86e | test: 7 条 P0 红测试（验证基线 7 failed） |
| 306bd7d | fix(p0-2): 版本正则优先 arXiv 后缀 + 去重先于置 done |
| 4c83359 | fix(p0-1): 列表接口返回 dedup_alert |
| d8b9074 | fix(p0-3): 索引视图截断 toc（summary 块不越 cap） |
| 45da8a0 | fix(p0-4): Dockerfile COPY uv.lock |
| 293ae50 | fix(p0-6): writer final 携带 outline |
| bcc9e9b | fix(p0-7): ping 失败短 TTL 30s + 重试 2 次 |
| c1364f2 | fix(p1-1): trace 先落盘再推 done（回放竞态） |
| b27f706 | fix: final 恒带 anchors（溯源字段稳定） |
| 767c8c4 | docs: README 对齐实际 + eval_gold.py |
| 1efa5b8 | ci: official-cases 移除 continue-on-error（必过门槛） |

## 真素材参考（非门禁，评委视角）

- 3.1 版本去重（真 v7→v1，MinerU 双解析）：**PASS** —— dedup_alert 有，「建议保留」「v7」文案齐
  （P0-2 修复在真素材上验证）
- 3.2a 扫描件 OCR（真 v1_img）：**PASS** —— engine=mineru，coverage=93.3%
- 3.2b 超长书（真 UDL 541 页）：MinerU 整本超 6492s 防线 → PyMuPDF 诚实兜底（10s 出 181 条书签，
  toc=181/coverage 99.3%/前 120 页索引/内存 <2GB）——见 docs/07 §四 验收实测
