# 前端改造方案 v1.0 验收清单（round1 · 2026-11-11）

- [x] 任意页面无 RISO/浅蓝混排（RISO 仅 ?theme=riso 品牌层）；无排期徽章；无 "D11" 字样（e2e ⑭ 断言）
- [x] 首页 3 秒测试：数据带 + 最近问答 + 图谱速览第一屏可读（截图 home-workbench.png）
- [x] 图谱：高度 664px ≥600、标签词典零截断（视觉核）、Hull 凸包启用、基石「基」章 badges、图例完整（graph-stage.png）
- [x] Chat：空态 1 次点击即发出演示问题（e2e ⑫ 拦截断言）；claim supported 显示「证」章（chat-answered.png）；E# mono+脉冲
- [x] 阅读器：五视图分段控件正常；旧 ①②⑥⑦⑨ e2e 全过（文案同 PR 同步：双语对照→双语 / DocIR 文本→DocIR）
- [x] /judge：8 步路线（e2e ⑭ 断言）+ 工程证据数字与 README 同步（pytest 286 · e2e 14 · eval 16/17 · 22 篇 991refs）
- [x] `pnpm build` 绿；`pnpm e2e` 14/14；pytest 286（test_tracker 2 例环境性失败为预存,与本轮无关,已用无改动复现佐证）
- [x] 视觉截图 7 张入 docs/acceptance/round1/screenshots/（首页/Chat 空态/回答中/回答完成/图谱/阅读器/评审导览）
- [x] README、design-tokens.md v1.2 已同步
- [ ] CI acceptance 绿（待 push 后确认——本地未推送）
- [ ] 可选项未实施（如实记录）：双语同步指示线（§6.2）、统一 Tooltip（§8.1）、快捷键 cheatsheet（§8.3）、速览力导向悬停（§8.4）；audit 数字 mono 未逐项替换（§7.4 低优先级遗留）
