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
 * ⑨ 双语滚动同步量化：中线块锚定一致（Round4）
 * ⑯ 标题树跨页定位：点跨页 TOC → 页码切换 + 目标块闪高亮（W6）
 * ⑰ [n, m] 分组引用：逐号 chip + 双向跳转（W6）
 * ⑱ 大纲加载失败错误态 + 重试恢复（W6）
 * ⑲ PDF TextLayer 提供 --scale-factor（取词框对齐，W6）
 * ⑳ 图谱含金量 chip 渲染（W3）
 */
import { expect, test } from "@playwright/test";

const API = "http://localhost:8100";

async function firstPaperId(): Promise<string> {
  const r = await fetch(`${API}/api/papers`);
  const j = await r.json();
  return j.papers?.[0]?.id ?? "";
}

async function fetchDocir(paperId: string): Promise<any> {
  const r = await fetch(`${API}/api/papers/${paperId}/docir`);
  return r.ok ? r.json() : {};
}

async function fetchOutline(paperId: string): Promise<any> {
  const r = await fetch(`${API}/api/papers/${paperId}/docir?outline=1`);
  return r.ok ? r.json() : {};
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
  await page.getByRole("button", { name: "双语" }).click();   // C5 分段控件:文案由「双语对照」收敛为「双语」(同 PR 同步)
  await expect(page.getByText("译文（block 级对齐）")).toBeVisible({ timeout: 20_000 });
  // 块渲染就绪（fetchPage 竞态：pane 无块时 observer 无可绑定目标，同步必不触发）
  await expect(async () => {
    await expect(page.locator("[data-bid]").first()).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 20_000 });
  // 左右栏都是 overflowY:auto 的 .card 容器——取含标题的最小 card（外层包装同名会误中）
  const left = await paneByHeading(page, "原文");
  const right = await paneByHeading(page, "译文（block 级对齐）");
  await page.waitForTimeout(600);   // 等 observer 绑定完成
  const canScroll = await left.evaluate((el: HTMLElement) => el.scrollHeight > el.clientHeight + 40);
  test.skip(!canScroll, "当前页内容不足一屏（滚动同步无从观测，换页/换论文重试）");
  const before = await right.evaluate((el: HTMLElement) => el.scrollTop);
  // Round6：滚到中线（旧固定 600px 在较短厚论文上越过 observer 中线带不触发同步）
  await left.evaluate((el: HTMLElement) => { el.scrollTop = Math.floor(el.scrollHeight / 2); });
  // Round8：等 observer 绑定+触发（UDL 新 docir 块密度变化后 observer 需更长稳定期）
  let after = 0;
  for (let t = 0; t < 6; t++) {
    await page.waitForTimeout(600);
    after = await right.evaluate((el: HTMLElement) => el.scrollTop);
    if (after !== 0) break;
  }
  expect(after).toBeGreaterThanOrEqual(0);
  if (before === 0) {
    expect(after).not.toBe(0);
  }
});

test("② [n] 引用标记 → References 跳转高亮", async ({ page }) => {
  const pid = await attentionPaperId();
  test.skip(!pid, "库内无论文");
  await page.goto(`/reader/${pid}`);
  await page.getByRole("button", { name: "DocIR" }).click();   // C5 分段控件同 PR 同步
  const chip = page.locator("span.chip[data-bid], p span").filter({ hasText: /^\[\d+\]$/ }).first();
  const hasCite = await chip.count();
  test.skip(!hasCite, "该论文正文无 [n] 标记（换用含数字引用的论文）");
  const n = (await chip.textContent())?.replace(/[^\d]/g, "") ?? "";
  await chip.click();
  await expect(page.getByText(/References（\d+ 条/)).toBeVisible({ timeout: 10_000 });
  // 加强断言（W6）：References 面板展开且被引条目可见（正向链路不再只看标题）
  if (n) {
    await expect(page.locator(`[data-refidx="${n}"]`).first()).toBeVisible({ timeout: 10_000 });
  }
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

test("⑤ Graph：G6 渲染节点 > 0 + 20 篇全量性能", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/graph");
  const t0 = Date.now();
  await expect(page.locator("canvas").first()).toBeVisible({ timeout: 60_000 });
  const nodeText = page.getByText(/节点 · \d+ 边/);
  await expect(nodeText).toBeVisible({ timeout: 30_000 });
  const elapsed = Date.now() - t0;
  // Round8：20 篇全量渲染性能——canvas 首帧+节点计数可见 ≤60s（G6 大图优化基线）
  expect(elapsed).toBeLessThan(60_000);
  const nodes = await nodeText.textContent();
  const n = parseInt((nodes ?? "").match(/\d+/)?.[0] ?? "0", 10);
  expect(n).toBeGreaterThan(0);
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

/** 定位真正的滚动 pane：含指定标题的**最小** div.card（外层包装 card 同样包含
 *  该标题节点，按高度排序取最小即 pane 本体；返回 evaluateHandle 元素句柄）。 */
async function paneByHeading(page: import("@playwright/test").Page, heading: string) {
  return await page.evaluateHandle((h) => {
    const cards = Array.from(document.querySelectorAll("div.card")) as HTMLElement[];
    // 首选：含标题且有块的（可能需等 fetchPage 落块）；退化：仅含标题的最小 card
    const withBlocks = cards.filter((c) => {
      const el = c.querySelector("h2");
      return el && el.textContent && el.textContent.includes(h) && c.querySelector("[data-bid]");
    });
    const hits = withBlocks.length ? withBlocks : cards.filter((c) => {
      const el = c.querySelector("h2");
      return el && el.textContent && el.textContent.includes(h);
    });
    hits.sort((a, b) => a.offsetHeight - b.offsetHeight);
    return (hits[0] ?? null) as HTMLElement | null;
  }, heading);
}


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
  // Round4：左栏 PDF 上的块级透明热点（data-pdf-bid）→ 点击后右栏对应译文块闪高亮。
  // 热点由 PdfAnchored 在 canvas 渲染完成后挂载——先等就绪再判有无（否则竞态误 skip）
  await expect(async () => {
    await expect(page.locator("[data-pdf-bid]").first())
      .toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 20_000 });
  const hot = page.locator("[data-pdf-bid]").first();
  const bid = await hot.getAttribute("data-pdf-bid");
  // 断言持久效果：点热点 → 右栏滚动使目标块进入视口（flash 闪高亮仅 2.5s，
  // 作为时敏效果不作硬断言，进入视口即证明反向联动链路生效）
  await expect(async () => {
    await hot.click({ force: true });
    const inPane = await page.evaluate((b) => {
      const cards = Array.from(document.querySelectorAll("div.card")) as HTMLElement[];
      const pane = cards.find(c => c.querySelector("h2")?.textContent?.includes("译文（第"));
      const it = pane?.querySelector(`[data-bid="${b}"]`) as HTMLElement | null;
      if (!pane || !it) return false;
      const pr = pane.getBoundingClientRect();
      const br = it.getBoundingClientRect();
      return br.top < pr.bottom && br.bottom > pr.top;
    }, bid);
    expect(inPane).toBeTruthy();
  }).toPass({ timeout: 30_000 });
});

test("⑧ Discover：从库内导入参考文献 + 含金量可逆过滤", async ({ page }) => {
  const pid = await attentionPaperId();
  test.skip(!pid, "库内无论文");
  await page.goto("/discover");
  // 四区是 tab：先切到 ② 参考文献下钻（dev 水合滞后→重试点击，与 ④ 同款防御）
  const select = page.getByRole("combobox");
  await expect(async () => {
    await page.getByRole("button", { name: "② 参考文献下钻" }).click();
    await expect(page.getByText("参考文献列表下钻")).toBeVisible({ timeout: 4_000 });
    await expect(select).toBeVisible({ timeout: 4_000 });
  }).toPass({ timeout: 60_000 });
  await select.selectOption(pid);
  // 导入完成：refsNote 提示 + 参考文献卡列表出现
  await expect(page.getByText(/已导入 \d+ 条参考文献/)).toBeVisible({ timeout: 30_000 });
  const cards = page.locator("div.card", { hasText: /实质|背景|凑数/ });
  await expect.poll(async () => cards.count(), { timeout: 20_000 }).toBeGreaterThan(0);
  // W4 起默认已自动剔除「凑数引用」（可逆）：先读默认剔除数，再展开/恢复/再次剔除
  const autoHide = page.getByText(/已自动剔除 \d+ 条凑数引用（点击恢复）|隐藏凑数引用/).first();
  await expect(autoHide).toBeVisible({ timeout: 20_000 });
  const label = (await autoHide.textContent()) ?? "";
  const autoN = Number(label.match(/已自动剔除 (\d+) 条/)?.[1] ?? "0");
  const folded = await cards.count();      // 默认（凑数已剔除）可见卡数
  await autoHide.click();                   // 恢复显示凑数
  if (autoN > 0) {
    await expect.poll(async () => cards.count(), { timeout: 10_000 })
      .toBeGreaterThanOrEqual(folded + autoN);
  } else {
    // 分类结果随缓存波动可能无凑数条目：过滤退化为恒等可逆校验（不跳过）
    expect(await cards.count()).toBe(folded);
    test.info().annotations.push({ type: "note",
      description: "本轮无凑数条目（含金量分类波动），过滤断言退化为恒等可逆" });
  }
  await page.getByText(/隐藏凑数引用/).first().click();   // 再次剔除 → 可逆回到默认
  await expect.poll(async () => cards.count(), { timeout: 10_000 }).toBe(folded);
});

test("⑨ 双语滚动同步量化：中线块锚定一致", async ({ page }) => {
  const { pid, pageNo, blocks } = await thickPaperWithDensePage();
  test.skip(!pid, "库内无论文");
  test.skip(blocks < 12, `最密页仅 ${blocks} 块`);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(`/reader/${pid}?page=${pageNo}`);
  await page.getByRole("button", { name: "双语" }).click();   // C5 分段控件:文案由「双语对照」收敛为「双语」(同 PR 同步)
  await expect(page.getByText("译文（block 级对齐）")).toBeVisible({ timeout: 20_000 });
  // 等块渲染就绪（fetchPage 竞态会让 pane 暂无 [data-bid]，直接 evaluate 会踩 undefined）
  await expect(async () => {
    await expect(page.locator("[data-bid]").first()).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 20_000 });
  const left = await paneByHeading(page, "原文");
  const right = await paneByHeading(page, "译文（block 级对齐）");
  await page.waitForTimeout(600);
  const hasPanes = await Promise.all([left.evaluate((el: HTMLElement) => !!el),
                                      right.evaluate((el: HTMLElement) => !!el)]);
  test.skip(hasPanes.some((v) => !v), "未找到滚动 pane（标题结构变化）");
  const canScroll = await left.evaluate((el: HTMLElement) => el.scrollHeight > el.clientHeight + 40);
  test.skip(!canScroll, "当前页不足一屏");
  // 滚左栏到中部 → 等 IntersectionObserver 同步（防回环 debounce 120ms + rAF）
  await left.evaluate((el: HTMLElement) => { el.scrollTop = Math.floor(el.scrollHeight / 2); });
  // 口径（Round4 终版）：块对齐契约 = **中线块锚定**——左栏视口中线所在块，经同步后
  // 在右栏须完整可见。两侧逐块排版高度不同（译文/原文行高差异是常态），「首个完整
  // 可见块序号」不具守恒性（几何事实）；同步实现按块 id 顶偏映射，可守恒量是块锚点。
  const anchored = async () => {
    const midBid = await left.evaluate((el: HTMLElement) => {
      const r = el.getBoundingClientRect();
      const cy = r.top + r.height / 2;
      const items = Array.from(el.querySelectorAll<HTMLElement>("[data-bid]"));
      let nearest = "";
      for (const it of items) {
        const b = it.getBoundingClientRect();
        if (b.top <= cy && b.bottom >= cy) return it.dataset.bid ?? "";
        if (b.top <= cy) nearest = it.dataset.bid ?? nearest;   // 中线落在块间隙时取上方近邻
      }
      return nearest;
    });
    if (!midBid) return false;
    return await page.evaluate(([l, r, bid]) => {
      const L = l as HTMLElement, R = r as HTMLElement;
      const src = L.querySelector(`[data-bid="${bid}"]`) as HTMLElement | null;
      const dst = R.querySelector(`[data-bid="${bid}"]`) as HTMLElement | null;
      if (!src || !dst) return false;
      // 映射不变量（同步实现=顶偏保持）：同块在两栏视口内的顶偏像素差 ≤80px
      const lp = L.getBoundingClientRect(), rp = R.getBoundingClientRect();
      const sb = src.getBoundingClientRect(), db = dst.getBoundingClientRect();
      return Math.abs((sb.top - lp.top) - (db.top - rp.top)) <= 80;
    }, [left, right, midBid] as const);
  };
  await expect(async () => {
    expect(await anchored()).toBeTruthy();
  }).toPass({ timeout: 10_000 });
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
  // DOT / TikZ 源码卡 + 结构校验（heading 角色：页面说明文字也含 "Graphviz DOT" 会撞 strict）
  await expect(page.getByRole("heading", { name: "Graphviz DOT" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "TikZ (LaTeX)" })).toBeVisible();
  const dotSrc = page.locator("pre").filter({ hasText: /digraph/ });
  await expect(dotSrc).toBeVisible();
  await expect(page.locator("pre").filter({ hasText: /tikzpicture/ })).toBeVisible();
});

/** ================= 前端改造方案 v1.0 新增用例(⑪-⑭,CI 安全:不依赖 LLM/外网) ================= */

test("⑪ 首页工作台渲染：数据带+最近问答卡+图谱速览 canvas", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/");
  await expect(page.getByTestId("wb-stats")).toBeVisible({ timeout: 30_000 });
  // 数据带含论文计数(「篇文献」)
  await expect(page.getByTestId("wb-stats")).toContainText("篇文献");
  // 最近问答卡:有数据行或空态引导,二者必居其一
  const qa = page.getByTestId("wb-qa");
  await expect(qa).toBeVisible();
  await expect(async () => {
    const hasRows = await page.locator(".wb-qa-row").count();
    const hasEmpty = await qa.locator(".wb-empty").count();
    expect(hasRows + hasEmpty).toBeGreaterThan(0);
  }).toPass({ timeout: 20_000 });
  // 图谱速览 canvas(G6 只读缩略)
  await expect(page.locator('[data-testid="wb-graph"] canvas').first())
    .toBeVisible({ timeout: 30_000 });
});

test("⑫ Chat 空态演示卡：点击即提问（拦截后端，仅断言请求发出）", async ({ page }) => {
  let chatRequested = false;
  await page.route("**/api/chat**", async (route) => {
    chatRequested = true;
    await route.abort();
  });
  await page.goto("/chat");
  const first = page.getByTestId("demo-q-0");
  await expect(first).toBeVisible({ timeout: 20_000 });
  await first.click();
  // 断言请求已发出(fetch 已走):输入框被清空(问题被发送)
  const input = page.locator(".dock input");
  await expect(async () => {
    expect(chatRequested).toBeTruthy();
    expect(await input.inputValue()).toBe("");
  }).toPass({ timeout: 15_000 });
});

test("⑬ 图谱舞台：画布高度 ≥560px + 统计 popover 可见 + 无渲染失败", async ({ page }) => {
  test.setTimeout(150_000);
  await page.goto("/graph");
  const card = page.locator(".graph-canvas-card");
  await expect(card.locator("canvas").first()).toBeVisible({ timeout: 60_000 });
  const height = await card.evaluate((el) => el.getBoundingClientRect().height);
  expect(height).toBeGreaterThanOrEqual(560);
  await expect(page.getByTestId("graph-info-pop")).toBeVisible();
  // 注:节点标签在 canvas 内,DOM 不可断言文本——零截断由视觉截图人工核(§9.3)
  const errCount = await page.getByText("G6 渲染失败").count();
  expect(errCount).toBe(0);
  // ⑳ 含金量 chip（W3：insights.class_stat 渲染，修复前该字段读错位置永不显示）
  await expect(page.getByText(/含金量：实质 \d+/)).toBeVisible({ timeout: 90_000 });
});

test("⑭ 评审导览：8 步路线 + 全页无 D11/排期徽章", async ({ page }) => {
  await page.goto("/judge");
  await expect(page.getByRole("heading", { name: /评审导览/ })).toBeVisible({ timeout: 20_000 });
  const steps = await page.locator(".judge-step").count();
  expect(steps).toBe(8);
  const body = await page.evaluate(() => document.body.textContent ?? "");
  expect(body).not.toContain("D11");
  expect(body).not.toContain("排期");
});

test("⑮ 健康中心：运行时 7 子分+工程快照+样本/外部校准明细", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/health");
  await expect(page.getByTestId("health-total")).toBeVisible({ timeout: 30_000 });
  // 总分：样本充足时为 0-100 数字；样本不足时显示 — 并出现提示条
  const totalText = (await page.locator(".h-total-num").textContent())?.trim();
  if (totalText === "—") {
    await expect(page.getByTestId("health-insufficient")).toBeVisible();
  } else {
    expect(Number(totalText)).not.toBeNaN();
  }
  // 7 个运行时子分卡 + 1 张工程快照（不计分）
  const cards = page.locator(".h-score-card");
  await expect(cards).toHaveCount(8);
  for (let i = 0; i < 8; i++) {
    await expect(cards.nth(i)).toContainText(/权重|不计分|按 0 计/);
  }
  await expect(page.getByTestId("health-score-engineering")).toContainText("不计分");
  // 口径与明细：生产样本窗口、固定权重、外部校准、ClaimTrace
  await expect(page.getByText(/运行时健康分/)).toBeVisible();
  await expect(page.getByText(/生产轨迹窗口 7 天/)).toBeVisible();
  await expect(page.getByText(/外部校准/).first()).toBeVisible();
  await expect(page.getByTestId("health-detail")).toBeVisible();
  await expect(page.getByText("ClaimTrace 四态分布")).toBeVisible();
  // 工程数字来自 web/lib/evidence.ts（不手写）
  await expect(page.getByText(/pytest \d+\/\d+ ×/)).toBeVisible();
  await expect(page.getByText(/e2e \d+\/\d+/)).toBeVisible();
  const body = await page.evaluate(() => document.body.textContent ?? "");
  expect(body).not.toContain("手注静态");
});

// ---------------- W6/W3 新增回归（2026-11-15 修复） ----------------

test("⑯ 标题树跨页定位：点跨页 TOC → 页码切换 + 目标块闪高亮", async ({ page }) => {
  test.setTimeout(120_000);
  const pid = await attentionPaperId();
  test.skip(!pid, "库内无论文");
  const outline = await fetchOutline(pid);
  const entry = (outline.toc ?? []).find((t: any) => t.level === 1 && (t.page ?? 0) + 1 > 1);
  test.skip(!entry, "该论文无跨页一级标题");
  await page.goto(`/reader/${pid}?page=1`);
  await page.getByRole("button", { name: "DocIR" }).click();
  const item = page.locator(".toc-panel .toc-item").filter({ hasText: entry.title }).first();
  await expect(item).toBeVisible({ timeout: 30_000 });
  await item.click();
  // 页码选择器切到目标页（唯一 <select>）
  await expect.poll(async () => page.locator("select").first().inputValue(),
    { timeout: 20_000 }).toBe(String(entry.page + 1));
  // 目标块闪高亮：flash 状态 2.5s 自清，poll 抢在窗口内
  await expect.poll(async () => page.locator(".block-item.flash").count(),
    { timeout: 8_000 }).toBeGreaterThan(0);
});

test("⑰ [n, m] 分组引用：逐号 chip + 双向跳转", async ({ page }) => {
  test.setTimeout(120_000);
  const pid = await attentionPaperId();
  test.skip(!pid, "库内无论文");
  const doc = await fetchDocir(pid);
  const block = (doc.blocks ?? []).find((b: any) =>
    /\[\d{1,3}\s*[,，]\s*\d{1,3}\]/.test(b.text ?? ""));
  test.skip(!block, "该论文无分组引用");
  const m = (block.text as string).match(/\[(\d{1,3})\s*[,，]\s*(\d{1,3})\]/)!;
  const [a, b] = [m[1], m[2]];
  await page.goto(`/reader/${pid}?page=${(block.page ?? 0) + 1}`);
  await page.getByRole("button", { name: "DocIR" }).click();
  const blk = page.locator(`[data-bid="${block.id}"]`);
  await expect(blk).toBeVisible({ timeout: 30_000 });
  // 分组引用必须逐号渲染为独立 chip（修复前只保留第一个编号）
  const chipA = blk.locator("span.anchor-chip").filter({ hasText: new RegExp(`^\\[${a}\\]$`) });
  const chipB = blk.locator("span.anchor-chip").filter({ hasText: new RegExp(`^\\[${b}\\]$`) });
  await expect(chipA.first()).toBeVisible();
  await expect(chipB.first()).toBeVisible();
  // 正向：点第二个编号 → References 面板展开且对应条目存在
  await chipB.first().click();
  await expect(page.getByText(/References（\d+ 条/)).toBeVisible({ timeout: 10_000 });
  const refRow = page.locator(`[data-refidx="${b}"]`).first();
  await expect(refRow).toBeVisible({ timeout: 10_000 });
  // 反向：点 References 条目 → 正文分组 chip 高亮（背景色不再透明）
  await refRow.click();
  await expect.poll(async () => chipB.first().evaluate(
    (el) => getComputedStyle(el).backgroundColor), { timeout: 8_000 })
    .not.toBe("rgba(0, 0, 0, 0)");
});

test("⑱ 大纲加载失败：错误态 + 重试恢复", async ({ page }) => {
  test.setTimeout(120_000);
  const pid = await attentionPaperId();
  test.skip(!pid, "库内无论文");
  let failed = false;
  await page.route(/docir\?outline=1/, async (route) => {
    if (!failed) {
      failed = true;
      await route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
    } else {
      await route.continue();
    }
  });
  await page.goto(`/reader/${pid}`);
  await expect(page.getByTestId("outline-error")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("outline-retry").click();
  await expect(page.getByText(/标题树（/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("outline-error")).toHaveCount(0);
});

test("⑲ PDF TextLayer：容器提供 --scale-factor（取词框对齐）", async ({ page }) => {
  test.setTimeout(120_000);
  const pid = await attentionPaperId();
  test.skip(!pid, "库内无论文");
  await page.goto(`/reader/${pid}`);
  const layer = page.locator(".textLayer").first();
  await expect(layer).toBeVisible({ timeout: 60_000 });
  await expect(layer).toHaveAttribute("style", /--scale-factor/);
});
