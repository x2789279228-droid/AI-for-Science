# Meridian 设计令牌标准 v1.6

> 全部 UI 的唯一取值依据。定义于 `web/app/tokens.css`，组件样式**只准引用 Semantic / Component 层**，禁止写死色值、字号、圆角。新增 token 必须先改本文档再改代码。
>
> **v1.6 变更**（皮肤体系保守优化）：① 新增 `--hl-border` 语义令牌（强调描边，classic=天蓝，三工作肤随母题换金/橙）；② `CANVAS` 画布色板新增 `text/textSoft/axisLine/splitLine` 四字段，ECharts 图例/轴文字完成深色适配（修 wuxia 图表文字隐形）；③ wuxia 补 `--shadow-sm/md/lg` 深色阴影覆盖（修深色下卡片失浮）；④ 主题知识收敛为单一清单 `web/lib/theme-registry.ts`（Theme/THEMES/CANVAS/mermaid 映射），`theme-palettes.ts` 降级为 re-export shim；⑤ 新增对比度门禁 `scripts/audit_theme_contrast.py`（FAIL <3.0 拦截 / WARN 3.0–4.5 记录，产出 `docs/acceptance/latest-verify/contrast.log`）。
>
> **v1.5 变更**（质感打磨）：`--ink-2` 次级文字 gray-500→**gray-600**（压灰提层）；三层阴影整体加重（sm 双层、md/lg 扩散半径与不透明度上调），卡片浮起更明确。
>
> **v1.4 变更**：新增第四皮肤 **WUXIA 玄墨夜行**（首个深色皮肤，武侠电影海报质感）；引入 `--accent-contrast` 语义令牌（亮 accent 上的对比文字色）；mermaid 画布按主题换肤；新增 `WuxiaTagline` 路由语录题跋与 `wuxia-quotes.ts` 语录表；插画挂载统一收敛为 `ThemeIllust` 三集分发。
>
> **v1.3 变更**：新增第三皮肤 **MAJORELLE 马拉喀什 · 蓝城花园**（第二工作皮肤）；恢复侧边栏「外观面板」切换器；画布图表（G6/ECharts）调色板按主题独立维护（`web/lib/theme-palettes.ts`）；动态岛/遮罩/选区/DiffLens 等散落硬编码色收敛为 Semantic 令牌。

## 分层结构

```
Primitive（原始阶：gray / blue 色板）
   └── Semantic（语义层：--bg / --ink / --accent …）  ← 组件唯一取色层
          └── Component（组件/布局层：--sidebar-w / --radius-md …）
```

## 1. Primitive

### 中性灰阶

| Token | 值 | | Token | 值 |
|---|---|---|---|---|
| `--gray-0` | `#ffffff` | | `--gray-400` | `#98a2b3` |
| `--gray-25` | `#fcfcfd` | | `--gray-500` | `#667085` |
| `--gray-50` | `#f7f8fa` | | `--gray-600` | `#475467` |
| `--gray-100` | `#f2f4f7` | | `--gray-700` | `#344054` |
| `--gray-200` | `#e4e7ec` | | `--gray-800` | `#1d2939` |
| `--gray-300` | `#d0d5dd` | | `--gray-900` | `#101828` |
| | | | `--gray-950` | `#0c111d` |

### 品牌蓝阶

| Token | 值 | | Token | 值 |
|---|---|---|---|---|
| `--blue-50` | `#eef3ff` | | `--blue-400` | `#6f93f8` |
| `--blue-100` | `#e0eaff` | | `--blue-500` | `#4a6ff2` |
| `--blue-200` | `#c2d3fe` | | `--blue-600` | `#2456e6` ← 主品牌 |
| `--blue-300` | `#9ab5fc` | | `--blue-700` | `#1d43b8` |

## 2. Semantic（组件唯一取色层）

| Token | 指向 | 用途 |
|---|---|---|
| `--bg` | `--gray-50` | 主区背景 |
| `--surface` | `--gray-0` | 卡片 / 侧边栏 / 输入 dock |
| `--surface-2` | `--gray-100` | 悬停态 / 嵌套色块 / 表头 |
| `--ink` | `--gray-900` | 主文字 / 图标 |
| `--ink-2` | `--gray-600` | 次级说明文字（v1.5：gray-500→600） |
| `--ink-3` | `--gray-400` | 占位 / 弱化 / 禁用 |
| `--line` | `--gray-200` | 常规边框、分隔线 |
| `--line-strong` | `--gray-300` | 输入框边、hover 边 |
| `--accent` / `--accent-hover` | `--blue-600/700` | 主按钮、链接、激活文字 |
| `--accent-soft` | `--blue-50` | 激活底、选中项背景 |
| `--accent-border` | `--blue-200` | 选中项边 |
| `--hl-border` | `--sky-400`（v1.6） | 强调描边（缝隙卡翻面等装饰性强调边；wuxia=鎏金 / majorelle=藏红花 / riso=国际橙） |
| `--accent-contrast` | `#ffffff`（v1.4） | 压在 --accent 上的文字色（深色皮肤覆盖为玄墨） |
| `--ok` / `--ok-soft` | `#067647` / `#e6f4ee` | 成功、已验证、服务健康 |
| `--warn` / `--warn-soft` | `#b25e09` / `#fff4e5` | 未验证论断、降级提示 |
| `--danger` / `--danger-soft` | `#d92d20` / `#fee4e2` | 错误、危险操作 |

## 3. Component / Layout

| 类别 | Token | 值 |
|---|---|---|
| 布局 | `--sidebar-w` | `272px` |
| 布局 | `--topbar-h` | `56px` |
| 圆角 | `--radius-sm / md / lg / full` | `6 / 10 / 14 / 999px` |
| 阴影 | `--shadow-sm` | `0 1px 2px rgba(16,24,40,.06), 0 1px 4px rgba(16,24,40,.05)`（wuxia 覆盖为纯黑深影，v1.6） |
| 阴影 | `--shadow-md` | `0 8px 24px -6px rgba(16,24,40,.14), 0 3px 8px -3px rgba(16,24,40,.07)`（wuxia 覆盖，v1.6） |
| 阴影 | `--shadow-lg` | `0 20px 44px -12px rgba(16,24,40,.24), 0 6px 14px -6px rgba(16,24,40,.10)`（wuxia 覆盖，v1.6） |
| 字号 | `--text-xs / sm / md / lg / xl / 2xl` | `12 / 13 / 14 / 15 / 17 / 20px` |
| 间距 | `--space-1~6, 8` | `4 / 8 / 12 / 16 / 20 / 24 / 32px` |
| 字体 | `--font` | Segoe UI / PingFang SC / Microsoft YaHei 栈 |

## 使用规则

1. **只引用 Semantic 层取色**：`.foo { color: var(--ink-2); border: 1px solid var(--line); }` ✅；`color: #667` ❌
2. **间距 / 圆角 / 字号用 Component 层**，不出现 magic number（1px 边、2px 微调除外）
3. **状态色语义**：论断「已验证」用 `--ok`，「未验证/降级」用 `--warn`，服务异常用 `--danger`
4. **深色模式预留**：未来只需覆盖 Semantic 层（Primitive 不动），组件代码零改动
5. 新增 token：先改本文档 → 再改 `tokens.css` → PR 说明用途
6. **画布图表例外（v1.3）**：G6 / ECharts 画布不走 CSS 变量，色板统一收敛在 `web/lib/theme-registry.ts` 的 `CANVAS[theme]`（v1.6 起 `theme-palettes.ts` 仅为 re-export shim），组件经 `canvasTheme(useShell().theme)` 取用；新增皮肤必须同时补画布色板并保持各主题图表语义一致（社区色环、边权三档、基石徽章、图例/轴文字 `text`/`axisLine`/`splitLine`——深色皮肤靠这组字段保证图表可读，v1.6）。
7. **主题单一清单（v1.6）**：`Theme` 类型、`THEMES` 元数据、`isTheme`、`CANVAS` 色板、`themeMermaid` 映射只准定义于 `web/lib/theme-registry.ts`；`ShellContext` 与各消费方经 import/re-export 取用，禁止再散落第二份皮肤枚举。`app/layout.tsx` 防闪烁内联脚本因水合前执行无法 import TS，是唯一豁免点——修改皮肤集合时必须同步其字面量。
8. **对比度门禁（v1.6）**：`scripts/audit_theme_contrast.py` 逐肤校验核心语义对（FAIL <3.0 / WARN 3.0–4.5），产出 `docs/acceptance/latest-verify/contrast.log`；新增皮肤或改 Semantic 取值后必须复跑，退出码非 0 不得合入。

## 4. RISO 品牌层（v1.2 降级；v1.3 起可经外观面板选用）

> **v1.3 修订（前端改造方案 §0.2）**：皮肤策略改为多皮肤——经典浅蓝 = 默认工作皮肤，MAJORELLE = 第二工作皮肤（§5），RISO 保持**品牌层**定位但恢复为可选皮肤。侧边栏「外观面板」（AppearancePanel）统一切换，`?theme=riso` 强制通道保留（查询参数优先于 localStorage 且不落盘）。业务组件禁止引用 `--riso-*`（品牌层组件 Stamp/Illustrations/BrandIntro 除外）。

### Primitive 新增（classic 不引用）

| Token | 值 | 用途 |
|---|---|---|
| `--riso-cream / --riso-paper / --riso-cream-2` | `#f4e9d6 / #faf1df / #eadfc6` | 米杏纸底/提亮纸面/纸面暗部 |
| `--riso-klein / --riso-klein-deep` | `#2233c9 / #1a2694` | Klein 蓝（主色）/ 蓝墨（已验证章） |
| `--riso-orange / --riso-orange-deep` | `#ff5f3c / #e64a26` | 国际橙（光源/强调）/ 橙墨（未验证章） |
| `--riso-vermilion` | `#d23a2e` | 朱砂（错误章） |
| `--riso-ink / --riso-warm-gray / --riso-warm-gray-3` | `#17161a / #5a564e / #938d80` | 墨黑 / 暖灰文字阶 |
| `--riso-line / --riso-line-strong / --riso-yellow` | `#e0d3b8 / #c9b995 / #ffd23f` | 纸边 / 海报黄点缀 |

### Semantic 覆盖（`[data-theme="riso"]` 块，组件零改动）

`--bg→米纸` `--surface→纸白` `--ink→墨黑` `--accent→Klein 蓝`；**状态色印章化**：`--ok→蓝墨(已验证)` `--warn→橙墨(未验证)` `--danger→朱砂(错误)`；`--grad-brand→蓝橙叠印`；`--radius-sm/md/lg→3/4/5px`（版画直角）；`--font-display→"Playfair Display", "Noto Serif SC", serif`。

### 新令牌

| Token | 用途 |
|---|---|
| `--font-serif-display` | 海报级衬线展示：`"Playfair Display", "Noto Serif SC", serif`（900 字重 + 斜排） |
| `--paper-noise` | feTurbulence 纸噪点 data-uri（body::after multiply 叠印） |
| `--halftone` | halftone 网点 background（currentColor 着色） |

### 艺术浓度边界（RISO 设计契约）

插图/叠印/斜排大字/印章只落**低密度区**（intro/书架/页头/空态/404/占位页/侧栏品牌区）；表格、正文、trace、chips 等**数据密集区只换色换纸感，不加装饰、对比度不降**。插图 = 自绘内联 SVG（components/Illustrations.tsx），仅 riso 主题渲染。

## 5. MAJORELLE 蓝城花园（v1.3 新增：第三皮肤 / 第二工作皮肤）

> **母题**：马拉喀什 · 马约尔花园 —— 马约尔蓝墙 × 藏红花黄 × 沙色麦地那 × zellige 几何花砖。审美调研基准：Jardin Majorelle 经典「蓝-绿-黄-玫瑰」组合。日常可用的欢快浅色工作皮肤，与 classic 同级。

### Primitive 新增（classic 不引用）

| Token | 值 | 用途 |
|---|---|---|
| `--maj-sand / --maj-sand-deep` | `#f6ecda / #efe1c8` | 沙色底 / 沙色暗部 |
| `--maj-surface` | `#fffaf1` | 米白面板（卡片/侧栏） |
| `--maj-blue / --maj-blue-deep` | `#4a4ae8 / #3232c8` | 马约尔蓝（主色）/ 蓝深（hover） |
| `--maj-blue-soft` | `#e9e9fb` | 淡蓝印底（选中底） |
| `--maj-saffron / --maj-saffron-deep` | `#e8b33c / #a87516` | 藏红花黄 / 可读藏红（文字级） |
| `--maj-rose` | `#d96c8a` | 骆驼玫瑰（点缀） |
| `--maj-palm / --maj-palm-deep` | `#3f7d4e / #2f6440` | 棕榈绿（ok 语义）/ 绿深 |
| `--maj-terra / --maj-terra-deep` | `#c96f4a / #a84e2e` | 陶土橙（danger 语义）/ 陶深 |
| `--maj-ink / --maj-ink-2 / --maj-ink-3` | `#33261a / #7a6a56 / #a3947e` | 椰枣褐墨 / 次级 / 弱化 |
| `--maj-line / --maj-line-strong` | `#e7d9bf / #d4c19e` | 暖沙线 / 强边 |

### Semantic 覆盖（`[data-theme="majorelle"]` 块，组件零改动）

`--bg→沙色` `--surface→米白` `--ink→椰枣褐` `--accent→马约尔蓝`；状态色：`--ok→棕榈绿` `--warn→藏红花深` `--danger→陶土深`；`--grad-brand→蓝→藏红花黄对角渐变`；`--glow→藏红花光晕`。

### 中性硬编码收敛层（v1.3 新增，classic 默认值 + 各主题覆盖）

| Token | classic 默认 | 用途 |
|---|---|---|
| `--island-bg / --island-ink / --island-ink-soft / --island-border` | 深墨蓝系 | 动态岛底/文字/弱文字/描边 |
| `--overlay / --overlay-soft` | `rgba(10,12,16,.32/.28)` | 全屏遮罩（抽屉/浮层） |
| `--sel` | `rgba(37,99,235,.25)` | PDF 文本层选区 |
| `--diff-add / --diff-add-ink` | `#2e8b57 / #236b44` | DiffLens 新增语义 |
| `--hl-insert / --hl-replace / --hl-delete` | `#d3f5ce / #ffe89e / #ffd2d2` | DiffLens 行内高亮 |

### 花砖纹理令牌

| Token | 用途 |
|---|---|
| `--zellige` | khatam 八角星花砖 data-uri（两方连续，body::after 低透明度叠印 / 卡片角标） |

### 插画集（v1.3 红线沿用）

插图 = 自绘内联 SVG（`components/MedinaIllustrations.tsx`，与 Illustrations.tsx 同名场景契约：`night-reader / paper-sea / lighthouse / peaks / magnify / observatory / press-house / wavy-flag`），仅 majorelle 主题渲染，色值全部 `var(--maj-*, 回退)`。数据密集区不加装饰的红线与 §4 一致。

### 画布色板

G6/ECharts 色板见 `web/lib/theme-palettes.ts` `CANVAS.majorelle`（社区色环以马约尔蓝起轮，边权三档 = 蓝深/暖沙/淡沙，基石徽章 = 陶土橙）。

## 6. WUXIA 玄墨夜行（v1.4 新增：第四皮肤 / 首个深色皮肤）

> **母题**：武侠电影海报——玄墨夜底 × 月白文字 × 鎏金线描侠客剪影 × 朱砂点睛。差异化定位：全套唯一深色皮肤，与三套浅色皮肤明暗互补。语录体系「经典名句为主」（`web/lib/wuxia-quotes.ts`），竖排题跋挂主区右上。艺术性优先：插画为侠客群像叙事，不受「数据密集区不加装饰」之外的克制约束（该红线仍守）。

### Primitive 新增（classic 不引用）

| Token | 值 | 用途 |
|---|---|---|
| `--wx-night / --wx-night-2 / --wx-night-3` | `#16130f / #1f1b16 / #2a241d` | 玄墨底 / 浓墨面板 / 墨面暗部 |
| `--wx-moon / --wx-moon-2 / --wx-moon-3` | `#ede6d6 / #a89c86 / #7a6f5c` | 月白文字阶（主/次/弱） |
| `--wx-cinnabar / --wx-cinnabar-2` | `#e06448 / #eb7a62` | 朱砂亮部（accent）/ 朱砂 hover 亮部 |
| `--wx-gold / --wx-gold-2` | `#d9a93c / #e8bd58` | 鎏金（题字/剑光/warn） |
| `--wx-bamboo / --wx-bamboo-2` | `#4fa97c / #7ccba4` | 竹青（ok）/ 竹青亮部 |
| `--wx-rouge / --wx-rouge-2` | `#e05a5a / #ec8080` | 胭脂（danger）/ 胭脂亮部 |
| `--wx-line / --wx-line-strong` | `#383026 / #4a4136` | 墨面线 / 强边 |

### Semantic 覆盖（`[data-theme="wuxia"]` 块，组件零改动）

`--bg→玄墨` `--surface→浓墨` `--ink→月白` `--accent→朱砂亮部`（hover 更亮——深色主题 hover 向亮走）；状态色：`--ok→竹青` `--warn→鎏金` `--danger→胭脂`；`--grad-brand→鎏金→朱砂`（海报题字渐变）；`--glow→鎏金晕`；`--claim-absent→淡墨`。

### 深色皮肤适配条款（v1.4 新增强制项，v1.6 扩充）

新增深色皮肤必须同时完成：① mermaid 主题按皮肤映射（`theme-registry.ts` 的 `themeMermaid`，wuxia→`"dark"`；消费于 `app/copilot/page.tsx` MermaidPreview，theme 入 deps）；② `.intro-overlay` 基础浅幕整体覆盖；③ 亮 accent 上的白字一律改 `var(--accent-contrast, #fff)`（新语义令牌：wuxia 覆盖为玄墨 `#16130f`，classic/majorelle/riso 保持白）；④ `CANVAS[theme]` 补 `text/textSoft/axisLine/splitLine` 并确认 ECharts 图例/轴/3D 轴标签全部接线（v1.6，防图表文字隐形）；⑤ `--shadow-sm/md/lg` 覆盖为深色深影（v1.6，防卡片失浮）。G6 标签 chip 保持深底白字（`CANVAS.wuxia.labelBg`）。

### 中性收敛层换装

`--island-bg→墨黑`（吉祥物三肤通用深底不变）+ `--island-border→鎏金`；`--overlay*→玄墨系`；`--sel→朱砂透明`；`--diff-add→竹青`；`--hl-insert/replace/delete→竹青/鎏金/胭脂 20% 透明色带`；`--stamp-verified→朱砂` `--stamp-cornerstone→鎏金` `--stamp-bg→浓墨`（语义不变：证=已验证，基=基石）。

### 纹理令牌

| Token | 用途 |
|---|---|
| `--ink-wash` | 月白低透明山影 data-uri（body::after 叠印 / 侧栏立言衬底） |

### 语录体系（v1.4 新增）

`web/lib/wuxia-quotes.ts`：`ROUTE_QUOTES`（路由→名句+出处，经典名句为主）+ `SCENE_QUOTES`（8 场景配文）。挂载：`WuxiaTagline` 竖排题跋（主区右上，仅 wuxia 渲染）、空态场景配文、侧栏立言、404 配文。**仅 wuxia 皮肤渲染，其他皮肤零影响。**

### 插画集

自绘内联 SVG（`components/WuxiaIllustrations.tsx`，同名场景契约，鎏金线描剪影 + 月白留白 + 朱砂点睛，原创主角原型），仅 wuxia 主题渲染。三套主题插画统一经 `components/ThemeIllust.tsx` 分发（riso/majorelle/wuxia 各自自守卫），挂载点不再感知皮肤数量。

### 画布色板

`CANVAS.wuxia`：社区色环以朱砂起轮的墨彩系（朱砂/鎏金/竹青/月白/黛青亮部）；标签 chip 深墨底白字；边权三档 = 朱砂/淡墨/墨线；visualMap = 月白→鎏金→朱砂。

## 7. 品牌印章语义（v1.2 新增）

| Token | 值 | 语义 |
|---|---|---|
| `--stamp-verified` | `var(--riso-klein-deep)` | 「证」章：claim 已验证 / 证据齐全 |
| `--stamp-cornerstone` | `var(--riso-orange-deep)` | 「基」章：图谱基石论文 |
| `--stamp-bg` | `var(--riso-paper)` | 章底色（米白纸） |
| `--stamp-border` | `1.5px solid` | 硬边（配 -2deg 微旋转） |

组件 `web/components/Stamp.tsx`：kind = verified/cornerstone；size = xs(16px 行内)/sm(22px 徽章)/md(36px 答辩材料)。
**红线**：仅此两种语义，禁止营销化滥用；同屏 ≤ 12 个。

## 8. ClaimTrace 四态语义色（v1.2 新增）

| Token | 指向 | 语义 |
|---|---|---|
| `--claim-ok` | `var(--ok)` | supported 已验证 |
| `--claim-check` | `var(--warn)` | needs_check 需核对 |
| `--claim-bad` | `var(--danger)` | unsupported 未验证 |
| `--claim-absent` | `#5b6b7c` | absent 未涉及（中性灰蓝，≠unsupported 红） |

## 9. 展示体语义变量与字号（v1.2 新增）

| Token | 用途 |
|---|---|
| `--font-display-cn` | 中文展示体：页面主标题(h1)/品牌词/印章字（Noto Serif SC） |
| `--font-display-en` | 西文展示体：品牌词 MERIDIAN/大图数字（Playfair Display） |
| `--font-data` | 数据体：E# 编号/页码/耗时/token 数/路径 chip（= --font-mono） |
| `--text-h1` | `26px`——页面主标题统一字号，禁止 20/22/24 混用 |

正文字体 = 系统 sans 栈（`--font`），正文禁用展示体。
