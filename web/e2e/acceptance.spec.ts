/**
 * 模块验收 E2E（Playwright，9 条关键用例）：
 * ① 阅读器双语对照滚动同步（拖左栏 → 右栏跟随）
 * ② [n] 引用标记 → References 面板跳转高亮（双向跳转正向链路）
 * ③ Copilot：CSV 上传 → 柱状图 canvas 渲染 + 图注展示
 * ④ Discover：多源搜索 SSE 完成（结果卡或错误提示如实出现）
 * ⑤ Graph：G6 canvas 渲染且节点 > 0
 * ⑥ PDF↔译文联动：点译文块 → PDF bbox 红框锚定
 * ⑦ PDF↔译文反向联动：点 PDF 块热点 → 译文定位闪高亮（Round4）
 * ⑧ Discover 从库内导入参考文献：含金量徽章 + 可逆「隐藏凑数」过滤（Round4）
 * ⑨ 双语滚动同步量化：块对齐误差 ≤1 块（Round4）
 */
import { expect, test } from "@playwright/test";

const API = "http://localhost:8100";

async function firstPaperId(): Promise<string> {
  const r = await fetch(`${API}/api/papers`);
  const j = await r.json();
  return j.papers?.[0]?.id ?? "";
}

async function attentionPaperId(): Promise<string> {
  const r = await fetch(`${API}/api/papers`);
  const j = await r.json();
  const hit = j.papers?.find((p: { title?: string }) =>
    (p.title ?? "").includes("Attention"));
  return hit?.id ?? j.papers?.[0]?.id ?? "";
}

test("① 双语对照：左栏滚动 → 右栏跟随", async ({ page }) => {
  const r = await fetch(`${API}/api/papers`);
  const j = await r.json();
  const thick = (j.papers ?? []).filter((p: { page_count?: number }) => (p.page_count ?? 0) > 50);
  const dense = thick.length
    ? thick.reduce((a: { page_count?: number }, b: { page_count?: number }) =>
        (b.page_count ?? 0) > (a.page_count ?? 0) ? b : a)
    : j.papers?.[0];
  const pid = dense?.id ?? "";
  test.skip(!pid, "库内无论文（先 make demo 导入语料）");
  // 经 API 找**最密**正文页（封面/目录/公式页不满一屏无法观测滚动）
  let pageNo = 1;
  let bestBlocks = 0;
  for (const p of [10, 20, 40, 60, 80, 100, 130]) {
    const resp = await fetch(`${API}/api/papers/${pid}/docir?page=${p - 1}`);
    if (!resp.ok) continue;
    const docir = await resp.json();
    const n = (docir.blocks ?? []).length;
    if (n > bestBlocks) { bestBlocks = n; pageNo = p; }
  }
  test.skip(bestBlocks < 12, `库内最密页仅 ${bestBlocks} 块，无法观测滚动同步`);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(`/reader/${pid}?page=${pageNo}`);
  await page.getByRole("button", { name: "双语对照" }).click();
  await expect(page.getByText("译文（block 级对齐）")).toBeVisible({ timeout: 20_000 });
  // 左右栏都是 overflowY:auto 的 .card 容器（按各自标题定位，避免命中外层包装）
  const left = page.locator("div.card", { has: page.getByRole("heading", { name: "原文" }) });
  const right = page.locator("div.card", { has: page.getByText("译文（block 级对齐）") });
  await page.waitForTimeout(600);   // 等 observer 绑定完成
  const canScroll = await left.evaluate((el) => el.scrollHeight > el.clientHeight + 40);
  test.skip(!canScroll, "当前页内容不足一屏（滚动同步无从观测，换页/换论文重试）");
  const before = await right.evaluate((el) => el.scrollTop);
  await left.evaluate((el) => { el.scrollTop = Math.min(600, el.scrollHeight / 2); });
  await page.waitForTimeout(1000);
  const after = await right.evaluate((el) => el.scrollTop);
  expect(after).toBeGreaterThanOrEqual(0);
  if (before === 0) {
    expect(after).not.toBe(0);
  }
});

test("② [n] 引用标记 → References 跳转高亮", async ({ page }) => {
  const pid = await attentionPaperId();
  test.skip(!pid, "库内无论文");
  await page.goto(`/reader/${pid}`);
  await page.getByRole("button", { name: "DocIR 文本" }).click();
  const chip = page.locator("span.chip[data-bid], p span").filter({ hasText: /^\[\d+\]$/ }).first();
  const hasCite = await chip.count();
  test.skip(!hasCite, "该论文正文无 [n] 标记（换用含数字引用的论文）");
  await chip.click();
  await expect(page.getByText(/References（\d+ 条/)).toBeVisible({ timeout: 10_000 });
});

test("③ Copilot：CSV → 柱状图 canvas + 图注", async ({ page }) => {
  await page.goto("/copilot");
  await page.getByRole("button", { name: /CSV 可视化/ }).click();
  const csv = "model,accuracy\nA,0.88\nB,0.92\nC,0.86\n";
  await page.locator('input[type="file"]').setInputFiles({
    name: "bench.csv", mimeType: "text/csv", buffer: Buffer.from(csv),
  });
  await expect(page.getByText(/已上传 bench.csv/)).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "柱状" }).click();
  await page.getByRole("button", { name: /生成图表 \+ 图注/ }).click();
  await expect(page.locator("canvas").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/Figure Caption：/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/0\.92/)).toBeVisible();   // 图注数字回溯
});

test("④ Discover：多源搜索完成（结果或错误如实）", async ({ page }) => {
  test.setTimeout(240_000);   // 外网三源往返余量
  await page.goto("/discover");
  await page.locator("input").first().fill("transformer attention");
  // dev 模式水合可能滞后（点击事件丢失）→ 重试点击直到状态行出现（水合完成的可观测信号）
  const btn = page.getByRole("button", { name: "多源检索" });
  await expect(async () => {
    await btn.click();
    await expect(page.getByText(/多源检索中|\[arxiv\]|检索完成|出错|请求失败/).first())
      .toBeVisible({ timeout: 4_000 });
  }).toPass({ timeout: 40_000 });
  await expect(
    page.getByText(/检索完成|出错|请求失败|离线回放/).first(),
  ).toBeVisible({ timeout: 200_000 });
});

test("⑤ Graph：G6 渲染节点 > 0", async ({ page }) => {
  await page.goto("/graph");
  await expect(page.locator("canvas").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/节点 · \d+ 边/)).toBeVisible({ timeout: 15_000 });
});

test("⑥ PDF↔译文联动：点译文块 → PDF bbox 红框锚定", async ({ page }) => {
  const r = await fetch(`${API}/api/papers`);
  const j = await r.json();
  const thick = (j.papers ?? []).filter((p: { page_count?: number }) => (p.page_count ?? 0) > 50);
  const dense = thick.length
    ? thick.reduce((a: { page_count?: number }, b: { page_count?: number }) =>
        (b.page_count ?? 0) > (a.page_count ?? 0) ? b : a)
    : j.papers?.[0];
  const pid = dense?.id ?? "";
  test.skip(!pid, "库内无论文");
  let pageNo = 1, bestBlocks = 0;
  for (const p of [10, 20, 40, 60, 80, 100, 130]) {
    const resp = await fetch(`${API}/api/papers/${pid}/docir?page=${p - 1}`);
    if (!resp.ok) continue;
    const docir = await resp.json();
    const n = (docir.blocks ?? []).length;
    if (n > bestBlocks) { bestBlocks = n; pageNo = p; }
  }
  await page.goto(`/reader/${pid}?page=${pageNo}`);
  await page.getByRole("button", { name: "PDF↔译文" }).click();
  await expect(page.getByText(/译文（第 \d+ 页/)).toBeVisible({ timeout: 20_000 });
  // 左 PDF canvas + 右译文块列并存
  await expect(page.locator("canvas").first()).toBeVisible();
  // 点击首个带 bbox 的译文块 → PDF 出现红框锚定（PdfAnchored 高亮 div，含 e5484d 边框色）
  const block = page.locator('[data-bid]').first();
  const hasBlock = await block.count();
  test.skip(!hasBlock, "该页无译文块");
  await expect(async () => {   // dev 水合可能滞后：重试点击直到锚定出现
    await block.click({ force: true });
    // React 内联样式 hex 会转 rgb()，故按 bboxPulse 动画特征定位红框
    await expect(page.locator('div[style*="bboxPulse"]').first())
      .toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 30_000 });
});

/** Round4 ⑦⑧⑨：反向联动 / discover 含金量过滤 / 滚动同步块对齐量化 */

async function thickPaperWithDensePage(): Promise<{ pid: string; pageNo: number; blocks: number }> {
  const r = await fetch(`${API}/api/papers`);
  const j = await r.json();
  const thick = (j.papers ?? []).filter((p: { page_count?: number }) => (p.page_count ?? 0) > 50);
  const dense = thick.length
    ? thick.reduce((a: { page_count?: number }, b: { page_count?: number }) =>
        (b.page_count ?? 0) > (a.page_count ?? 0) ? b : a)
    : j.papers?.[0];
  const pid = dense?.id ?? "";
  if (!pid) return { pid: "", pageNo: 1, blocks: 0 };
  let pageNo = 1, bestBlocks = 0;
  for (const p of [10, 20, 40, 60, 80, 100, 130]) {
    const resp = await fetch(`${API}/api/papers/${pid}/docir?page=${p - 1}`);
    if (!resp.ok) continue;
    const docir = await resp.json();
    const n = (docir.blocks ?? []).length;
    if (n > bestBlocks) { bestBlocks = n; pageNo = p; }
  }
  return { pid, pageNo, blocks: bestBlocks };
}

test("⑦ PDF↔译文反向联动：点 PDF 块热点 → 译文定位闪高亮", async ({ page }) => {
  const { pid, pageNo } = await thickPaperWithDensePage();
  test.skip(!pid, "库内无论文");
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(`/reader/${pid}?page=${pageNo}`);
  await page.getByRole("button", { name: "PDF↔译文" }).click();
  await expect(page.getByText(/译文（第 \d+ 页/)).toBeVisible({ timeout: 20_000 });
  await expect(page.locator("canvas").first()).toBeVisible();
  // Round4：左栏 PDF 上的块级透明热点（data-pdf-bid）→ 点击后右栏对应译文块闪高亮
  const hot = page.locator("[data-pdf-bid]").first();
  test.skip(!(await hot.count()), "该页无块热点（无 bbox 块）");
  const bid = await hot.getAttribute("data-pdf-bid");
  await expect(async () => {
    await hot.click({ force: true });
    await expect(page.locator(`[data-bid="${bid}"].flash`)).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 30_000 });
});

test("⑧ Discover：从库内导入参考文献 + 含金量可逆过滤", async ({ page }) => {
  const pid = await attentionPaperId();
  test.skip(!pid, "库内无论文");
  await page.goto("/discover");
  const select = page.getByRole("combobox");
  await expect(select).toBeVisible({ timeout: 15_000 });
  await select.selectOption(pid);
  // 导入完成：refsNote 提示 + 参考文献卡列表出现
  await expect(page.getByText(/已导入 \d+ 条参考文献/)).toBeVisible({ timeout: 30_000 });
  const cards = page.locator("div.card", { hasText: /实质|背景|凑数/ });
  const total = await cards.count();
  expect(total).toBeGreaterThan(0);
  // 凑数条目存在时：隐藏 → 卡数减少 → 点击恢复 → 卡数还原（可逆）
  const cheap = page.getByText("凑数", { exact: true });
  const nCheap = await cheap.count();
  test.skip(nCheap === 0, "该论文无「凑数」条目（引用句覆盖下无低权重引用）");
  await page.getByText("隐藏凑数引用").click();
  const hidden = await cards.count();
  expect(hidden).toBeLessThan(total);
  await page.getByText("已隐藏凑数（点击恢复）").click();
  await expect(async () => {
    expect(await cards.count()).toBe(total);
  }).toPass({ timeout: 10_000 });
});

test("⑨ 双语滚动同步量化：块对齐误差 ≤1 块", async ({ page }) => {
  const { pid, pageNo, blocks } = await thickPaperWithDensePage();
  test.skip(!pid, "库内无论文");
  test.skip(blocks < 12, `最密页仅 ${blocks} 块`);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(`/reader/${pid}?page=${pageNo}`);
  await page.getByRole("button", { name: "双语对照" }).click();
  await expect(page.getByText("译文（block 级对齐）")).toBeVisible({ timeout: 20_000 });
  const left = page.locator("div.card", { has: page.getByRole("heading", { name: "原文" }) });
  const right = page.locator("div.card", { has: page.getByText("译文（block 级对齐）") });
  await page.waitForTimeout(600);
  const canScroll = await left.evaluate((el) => el.scrollHeight > el.clientHeight + 40);
  test.skip(!canScroll, "当前页不足一屏");
  // 滚左栏到中部 → 等 IntersectionObserver 同步（防回环 debounce 120ms + smooth）
  await left.evaluate((el) => { el.scrollTop = el.scrollHeight / 2; });
  await page.waitForTimeout(1_500);
  // 各栏首个「完整可见」块的序号（data-bid 顺序）差 ≤1 即对齐
  const firstVisibleIdx = (el: HTMLElement) => {
    const pane = el.getBoundingClientRect();
    const items = Array.from(el.querySelectorAll<HTMLElement>("[data-bid]"));
    for (let i = 0; i < items.length; i++) {
      const b = items[i].getBoundingClientRect();
      if (b.top >= pane.top && b.bottom <= pane.bottom) return i;
    }
    return -1;
  };
  const li = await left.evaluate(firstVisibleIdx);
  const ri = await right.evaluate(firstVisibleIdx);
  expect(li).toBeGreaterThanOrEqual(0);
  expect(Math.abs(li - ri)).toBeLessThanOrEqual(1);
});

test("⑩ Copilot 拓扑图：四格式真渲染（Mermaid svg + DOT/TikZ 源码 + PNG）", async ({ page }) => {
  test.setTimeout(180_000);
  await page.goto("/copilot");
  await page.getByRole("button", { name: /拓扑图脚本/ }).click();
  await page.locator("textarea").first().fill(
    "RAG 系统：Query 改写器 → Retriever 从向量库取 top-k → Reranker 精排 → Generator 生成带引用回答");
  await page.getByRole("button", { name: "生成四种脚本" }).click();
  // Mermaid 客户端真渲染出 svg（MermaidPreview 组件）
  await expect(page.locator("div.card svg").first()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("语法通过")).toBeVisible({ timeout: 10_000 });
  // DOT / TikZ 源码卡 + 结构校验
  await expect(page.getByText("Graphviz DOT")).toBeVisible();
  await expect(page.getByText("TikZ (LaTeX)")).toBeVisible();
  const dotSrc = page.locator("pre").filter({ hasText: /digraph/ });
  await expect(dotSrc).toBeVisible();
  await expect(page.locator("pre").filter({ hasText: /tikzpicture/ })).toBeVisible();
});
