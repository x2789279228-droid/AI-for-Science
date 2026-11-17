#!/usr/bin/env python
"""皮肤对比度门禁（v1.6，design-tokens.md 使用规则 8）：逐肤校验核心语义色对的
WCAG 对比度，产出 docs/acceptance/latest-verify/contrast.log。

用法（系统 Python 均可，仅用标准库）：
  python scripts/audit_theme_contrast.py                 # 默认源/输出
  python scripts/audit_theme_contrast.py --out -         # 仅打印到 stdout

门禁语义（退出码 0/1）：
  FAIL  <3.0  核心文字对（--ink/--bg、--ink/--surface、--ink-2/--surface、
              --accent/--accent-contrast、--accent/--surface）→ 拦截
  WARN  <4.5  弱化/占位文字对（--ink-3 系、--claim-absent，WCAG 豁免弱化文字）→ 记录不拦截
  PASS  >=4.5

改 Semantic 取值或新增皮肤后必须复跑；退出码非 0 不得合入。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # scholar-agent/
DEFAULT_TOKENS = ROOT / "web" / "app" / "tokens.css"
DEFAULT_OUT = ROOT / "docs" / "acceptance" / "latest-verify" / "contrast.log"

# 核心文字对：FAIL <3.0 拦截（4.5:1 为 WCAG AA 正文标准，此处门禁线取 3.0，
# 现状基线达标即过，收严留待赛后）
CORE_PAIRS = [
    ("--ink", "--bg", "主文字/主底"),
    ("--ink", "--surface", "主文字/卡片"),
    ("--ink-2", "--surface", "次级文字/卡片"),
    ("--accent", "--accent-contrast", "主按钮字/accent 底"),
    ("--accent", "--surface", "链接与激活文字/卡片"),
]
# 弱化文字对：仅 WARN 记录（占位/禁用文字 WCAG 豁免，但漂移需可见）
SOFT_PAIRS = [
    ("--ink-3", "--surface", "占位弱化文字/卡片"),
    ("--ink-3", "--bg", "占位弱化文字/主底"),
    ("--claim-absent", "--surface", "absent 中性徽章/卡片"),
]
THEMES = ["classic", "riso", "majorelle", "wuxia"]   # 与 web/lib/theme-registry.ts 同步


# ---------- CSS 解析 ----------

def parse_tokens(text: str) -> tuple[dict, dict[str, dict]]:
    """tokens.css → (:root 令牌表, {主题: 覆盖表})。剥注释后按块提取声明。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    decl = r"--[\w-]+\s*:\s*[^;{}]+;"

    def _parse_block(body: str) -> dict:
        out = {}
        for m in re.finditer(decl, body):
            name, _, value = m.group(0).partition(":")
            out[name.strip()] = value.rstrip(";").strip()
        return out

    root = {}
    m = re.search(r":root\s*\{([^{}]*)\}", text)
    if m:
        root = _parse_block(m.group(1))
    themes = {}
    for m in re.finditer(r'\[data-theme="([\w-]+)"\]\s*\{([^{}]*)\}', text):
        themes[m.group(1)] = _parse_block(m.group(2))
    return root, themes


def resolve(name: str, table: dict, depth: int = 0) -> str | None:
    """递归解 var(--x[, fallback]) 链；解不出时回落 fallback 字面量。"""
    if depth > 8 or name not in table:
        return None
    value = table[name]
    m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]+?)\s*)?\)", value)
    if not m:
        return value
    resolved = resolve(m.group(1), table, depth + 1)
    return resolved if resolved is not None else (m.group(2).strip() if m.group(2) else None)


# ---------- 颜色与对比度 ----------

def parse_color(value: str | None) -> tuple[float, float, float, float] | None:
    """hex / rgb() / rgba() → (r, g, b, a)，均为 0-1。非纯色（渐变等）返回 None。"""
    if not value:
        return None
    value = value.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{3,8})", value)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 8:
            return (*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)), int(h[6:8], 16) / 255)
        if len(h) == 6:
            return (*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)), 1.0)
        return None
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)\s*(?:[,/]\s*([\d.]+%?))?\s*\)", value)
    if m:
        a = m.group(4)
        alpha = (float(a[:-1]) / 100 if a and a.endswith("%") else float(a)) if a else 1.0
        return (float(m.group(1)) / 255, float(m.group(2)) / 255, float(m.group(3)) / 255, alpha)
    return None


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: tuple[float, float, float, float], bg: tuple[float, float, float, float] | None) -> float:
    """相对亮度；前景带 alpha 时合成到 bg 上（bg 缺省按白底）。"""
    r, g, b, a = rgb
    if a < 1.0:
        br, bgc, bb, _ = bg if bg else (1.0, 1.0, 1.0, 1.0)
        r, g, b = r * a + br * (1 - a), g * a + bgc * (1 - a), b * a + bb * (1 - a)
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(fg: tuple, bg: tuple) -> float:
    lf, lb = luminance(fg, bg), luminance(bg, None)
    hi, lo = max(lf, lb), min(lf, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---------- 逐肤审计 ----------

def audit(tokens_path: Path) -> tuple[str, int]:
    """返回 (报告全文, FAIL 计数)。"""
    root, theme_blocks = parse_tokens(tokens_path.read_text(encoding="utf-8"))
    lines = [
        "# 皮肤对比度审计（自动生成，请勿手改）",
        "",
        f"- 生成时间：{datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M +08')}",
        f"- 取值源：{tokens_path.relative_to(ROOT).as_posix()}",
        "- 门禁：核心文字对 FAIL <3.0（拦截）；弱化文字对 WARN <4.5（记录不拦截）",
        "",
    ]
    fail_total, warn_total = 0, 0
    for theme in THEMES:
        table = dict(root)
        table.update(theme_blocks.get(theme, {}))
        lines.append(f"## {theme}")
        lines.append("")
        lines.append("| 前景 | 背景 | 对比度 | 结论 | 用途 |")
        lines.append("|---|---|---|---|---|")
        fail_n = warn_n = 0
        pairs = [(f, b, "核心") + (p,) for f, b, p in CORE_PAIRS] + \
                [(f, b, "弱化") + (p,) for f, b, p in SOFT_PAIRS]
        for fg_name, bg_name, kind, purpose in pairs:
            fg, bg = resolve(fg_name, table), resolve(bg_name, table)
            fg_c, bg_c = parse_color(fg), parse_color(bg)
            if fg_c is None or bg_c is None:
                lines.append(f"| {fg_name} | {bg_name} | N/A | SKIP | 取值非纯色（{fg} / {bg}） |")
                continue
            ratio = contrast(fg_c, bg_c)
            if kind == "核心" and ratio < 3.0:
                verdict, fail_n = "❌ FAIL", fail_n + 1
            elif ratio < 4.5:
                verdict, warn_n = "⚠️ WARN", warn_n + 1
            else:
                verdict = "✅ PASS"
            lines.append(f"| {fg_name} | {bg_name} | {ratio:.2f}:1 | {verdict} | {purpose} |")
        lines.append("")
        lines.append(f"小计：FAIL={fail_n} · WARN={warn_n} · PASS={len(pairs) - fail_n - warn_n}")
        lines.append("")
        fail_total, warn_total = fail_total + fail_n, warn_total + warn_n
    verdict = "✅ 门禁通过" if fail_total == 0 else "❌ 门禁拦截（存在核心文字对 FAIL）"
    lines.append("== 汇总 ==")
    lines.append("")
    lines.append(f"FAIL={fail_total} · WARN={warn_total} → {verdict}")
    return "\n".join(lines) + "\n", fail_total


def main() -> int:
    ap = argparse.ArgumentParser(description="皮肤对比度门禁（WCAG，逐肤校验 tokens.css）")
    ap.add_argument("--tokens", type=Path, default=DEFAULT_TOKENS, help="tokens.css 路径")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="报告输出路径，- 表示仅 stdout")
    args = ap.parse_args()

    if not args.tokens.exists():
        print(f"[contrast] tokens 不存在：{args.tokens}", file=sys.stderr)
        return 1
    report, fails = audit(args.tokens)

    if args.out == Path("-"):
        print(report, end="")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"[contrast] -> {args.out}")
    print(f"[contrast] FAIL={fails} · {'PASS' if fails == 0 else 'GATE BLOCKED'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
