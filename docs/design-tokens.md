# Meridian 设计令牌标准 v1.1

> 全部 UI 的唯一取值依据。定义于 `web/app/tokens.css`，组件样式**只准引用 Semantic / Component 层**，禁止写死色值、字号、圆角。新增 token 必须先改本文档再改代码。

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
| `--ink-2` | `--gray-500` | 次级说明文字 |
| `--ink-3` | `--gray-400` | 占位 / 弱化 / 禁用 |
| `--line` | `--gray-200` | 常规边框、分隔线 |
| `--line-strong` | `--gray-300` | 输入框边、hover 边 |
| `--accent` / `--accent-hover` | `--blue-600/700` | 主按钮、链接、激活文字 |
| `--accent-soft` | `--blue-50` | 激活底、选中项背景 |
| `--accent-border` | `--blue-200` | 选中项边 |
| `--ok` / `--ok-soft` | `#067647` / `#e6f4ee` | 成功、已验证、服务健康 |
| `--warn` / `--warn-soft` | `#b25e09` / `#fff4e5` | 未验证论断、降级提示 |
| `--danger` / `--danger-soft` | `#d92d20` / `#fee4e2` | 错误、危险操作 |

## 3. Component / Layout

| 类别 | Token | 值 |
|---|---|---|
| 布局 | `--sidebar-w` | `272px` |
| 布局 | `--topbar-h` | `56px` |
| 圆角 | `--radius-sm / md / lg / full` | `6 / 10 / 14 / 999px` |
| 阴影 | `--shadow-sm` | `0 1px 2px rgba(16,24,40,.05)` |
| 阴影 | `--shadow-md` | `0 4px 12px -2px rgba(16,24,40,.08), 0 2px 4px -2px rgba(16,24,40,.04)` |
| 字号 | `--text-xs / sm / md / lg / xl / 2xl` | `12 / 13 / 14 / 15 / 17 / 20px` |
| 间距 | `--space-1~6, 8` | `4 / 8 / 12 / 16 / 20 / 24 / 32px` |
| 字体 | `--font` | Segoe UI / PingFang SC / Microsoft YaHei 栈 |

## 使用规则

1. **只引用 Semantic 层取色**：`.foo { color: var(--ink-2); border: 1px solid var(--line); }` ✅；`color: #667` ❌
2. **间距 / 圆角 / 字号用 Component 层**，不出现 magic number（1px 边、2px 微调除外）
3. **状态色语义**：论断「已验证」用 `--ok`，「未验证/降级」用 `--warn`，服务异常用 `--danger`
4. **深色模式预留**：未来只需覆盖 Semantic 层（Primitive 不动），组件代码零改动
5. 新增 token：先改本文档 → 再改 `tokens.css` → PR 说明用途

## 4. RISO 主题（第二皮肤，v1.1 增补）

> 基调：创新/艺术/大胆。母题 = riso 双色印刷海报（Klein 蓝 × 国际橙 × 米杏纸 × 墨黑、halftone 网点、粗衬线斜排、硬阴影版画）。启用方式：`html[data-theme="riso"]`（ShellContext 持久化于 `localStorage.meridian:theme`）。

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
